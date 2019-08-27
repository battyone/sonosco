import logging
import math
import torch
import torch.nn as nn
import torch.nn.functional as torch_functional
import numpy as np

from collections import OrderedDict
from typing import List, Tuple, Dict, Any
from dataclasses import field

from sonosco.model.serialization import serializable
from .modules import SubsampleBlock, TDSBlock, Linear, BatchRNN, InferenceBatchSoftmax, supported_rnns
from .attention import DotAttention
from sonosco.config.global_settings import CUDA_ENABLED
from sonosco.common.utils import labels_to_dict


LOGGER = logging.getLogger(__name__)
EOS = '$'
PADDING_VALUE = '%'
MAX_LEN = 100


@serializable
class TDSEncoder(nn.Module):
    """TDS (time-depth separable convolutional) encoder.
    Args:
        input_dim (int) dimension of input features (freq * channel)
        in_channel (int) number of channels of input features
        channels (list) number of channels in TDS layers
        kernel_sizes (list) size of kernels in TDS layers
        strides (list): strides in TDS layers
        poolings (list) size of poolings in TDS layers
        dropout (float) probability to drop nodes in hidden-hidden connection
        batch_norm (bool): if True, apply batch normalization
        bottleneck_dim (int): dimension of the bottleneck layer after the last layer
    """
    input_dim: int
    in_channel: int
    dropout: float
    bottleneck_dim: int
    channels: List[int] = field(default_factory=list)
    kernel_sizes: List[int] = field(default_factory=list)

    def __post_init__(self):
        assert self.input_dim % self.in_channel == 0
        assert len(self.channels) > 0
        assert len(self.channels) == len(self.kernel_sizes)

        super().__init__()

        self.input_freq = self.input_dim // self.in_channel
        self.bridge = None

        layers = OrderedDict()
        in_ch = self.in_channel
        in_freq = self.input_freq
        subsample_factor = 1

        for layer, (channel, kernel_size) in enumerate(zip(self.channels, self.kernel_sizes)):
            # subsample
            if in_ch != channel:
                layers['subsample%d' % layer] = SubsampleBlock(in_channel=in_ch,
                                                               out_channel=channel,
                                                               in_freq=in_freq,
                                                               dropout=self.dropout)

                subsample_factor *= 2

            # Conv
            layers['tds%d_block%d' % (channel, layer)] = TDSBlock(channel=channel,
                                                                  kernel_size=kernel_size,
                                                                  in_freq=in_freq,
                                                                  dropout=self.dropout)

            in_ch = channel

        self._output_dim = int(in_ch * in_freq)

        if self.bottleneck_dim > 0:
            self.bridge = Linear(self._output_dim, self.bottleneck_dim)
            self._output_dim = self.bottleneck_dim

        self.layers = nn.Sequential(layers)

        # Initialize parameters
        self.subsample_factor = subsample_factor
        self.reset_parameters()

    def reset_parameters(self):
        """Initialize parameters with uniform distribution."""
        LOGGER.debug('===== Initialize %s =====' % self.__class__.__name__)
        for n, p in self.named_parameters():
            if p.dim() == 1:
                nn.init.constant_(p, val=0)  # bias
                LOGGER.debug('Initialize %s with %s / %.3f' % (n, 'constant', 0))
            elif p.dim() == 2:
                fan_in = p.size(1)
                nn.init.uniform_(p, a=-math.sqrt(4 / fan_in), b=math.sqrt(4 / fan_in))  # linear weight
                LOGGER.debug('Initialize %s with %s / %.3f' % (n, 'uniform', math.sqrt(4 / fan_in)))
            elif p.dim() == 4:
                fan_in = p.size(1) * p[0][0].numel()
                nn.init.uniform_(p, a=-math.sqrt(4 / fan_in), b=math.sqrt(4 / fan_in))  # conv weight
                LOGGER.debug('Initialize %s with %s / %.3f' % (n, 'uniform', math.sqrt(4 / fan_in)))
            else:
                raise ValueError

    def forward(self, xs, xlens):
        """Forward computation.
        Args:
            xs (FloatTensor): `[B, T, input_dim (+Δ, ΔΔ)]`
            xlens (list): A list of length `[B]`
        Returns:
            xs (FloatTensor): `[B, T', out_ch * feat_dim]`
            xlens (list): A list of length `[B]`
        """
        bs, time, input_dim = xs.size()
        xs = xs.contiguous().view(bs, time, self.in_channel, input_dim // self.in_channel).transpose(2, 1)
        # `[B, in_ch, T, input_dim // in_ch]`

        xs = self.layers(xs)  # `[B, out_ch, T, feat_dim]`
        bs, out_ch, time, freq = xs.size()
        xs = xs.transpose(2, 1).contiguous().view(bs, time, -1)  # `[B, T, out_ch * feat_dim]`

        # Bridge layer
        if self.bridge is not None:
            xs = self.bridge(xs)

        # Update xlens
        xlens /= self.subsample_factor

        return xs, xlens


@serializable
class TDSDecoder(nn.Module):
    labels:  str
    input_dim: int = 1024
    embedding_dim: int = 512
    key_dim: int = 512
    value_dim: int = 512
    rnn_hidden_dim: int = 512
    rnn_type_str: str = "gru"
    attention_type: str = "dot"
    sampling_prob: float = 0

    def __post_init__(self):
        assert self.input_dim == self.key_dim + self.value_dim
        assert self.rnn_hidden_dim == self.key_dim

        super().__init__()

        if EOS not in self.labels and PADDING_VALUE not in self.labels:
            self.labels = self.labels + EOS + PADDING_VALUE

        self.labels_map = labels_to_dict(self.labels)
        self.vocab_dim = len(self.labels)

        self.rnn_type = supported_rnns[self.rnn_type_str]

        self.word_piece_embedding = nn.Embedding(self.vocab_dim, self.embedding_dim)

        self.rnn = BatchRNN(input_size=self.embedding_dim, hidden_size=self.rnn_hidden_dim,
                            rnn_type=self.rnn_type, batch_norm=False)

        self.attention = DotAttention(self.key_dim)

        self.output_mlp = Linear(in_size=self.value_dim + self.rnn_hidden_dim, out_size=self.vocab_dim)

        self.inference_softmax = InferenceBatchSoftmax()

    def forward(self, encoding, encoding_lens, y_labels=None, y_lens=None):
        """
        Performs teacher-forcing inference if y_labels and y_lens are given, otherwise
        step-by-step inference while feeding the previously generated output into the rnn.

        :param encoding: [B,T,E]
        :param encoding_lens: len(encoding_lens)=B
        :param y_labels: [B,T,Y]
        :param y_lens: len(y_lens)=B
        :return: probabilities and lengths
        """
        # split into keys and values
        # keys [B,T,K], values [B,T,V]
        keys, values = torch.split(encoding, [self.key_dim, self.value_dim], dim=-1)

        if y_labels is not None and y_lens is not None:
            return self.__forward_train(keys, values, encoding_lens, y_labels, y_lens)
        else:
            return self.__forward_inference(keys, values, encoding_lens)

    def _random_sampling(self, y_labels):
        '''
        Randomly sample tokens given a specified probability, in order to bring
        training closer to inference.

        pseudo:
        1. sample U random numbers c from uniform distribution (0,1) [B, T, c]
        2. create vector of 1 and 0 with c > specified probability [B, T, 1 or 0]
        3. Sample vector Z of tokens (uniform distribution over tokens excl. eos) [B,T,token]
        4. Calc: Y_hat = R o Z + (1-R) o Y (Y being teacher forced tokens)

        :param y_labels: (torch tensor) [B, T, V] - tensor of groundtruth tokens
        :return: tensor of tokens, partially groundtruth partially sampled
        '''
        C = np.random.random_sample(size=y_labels.size)
        C[C>self.sampling_prob] = 1
        C[C<self.sampling_prob] = 0
        R = torch.from_numpy(C)

        Z = np.random.uniform(low=0, high=len(self.labels[:])-2, size=y_labels.size)
        Z = torch.from_numpy(Z)
        ones = torch.ones(y_labels.size)

        y_sampled = R*Z + (ones-R)*y_labels

        return y_sampled

    @staticmethod
    def __create_mask(inp, pad_idx):
        mask = (inp != pad_idx).permute(1, 0, 2)
        return mask

    def __forward_train(self, keys, values, encoding_lens, y_labels, y_lens):
        y_sampled = self._random_sampling(y_labels)
        # embed value that we get from random sampling

        y_embed = self.word_piece_embedding(y_sampled)
        y_embed = y_embed.transpose(0, 1).contiguous()  # TxBxD
        queries = self.rnn(y_embed, y_lens)
        queries = queries.transpose(0, 1)

        # summaries [B,T_dec,V], scores [B,T_dec,T_enc]
        # TODO: add encoding_lens for attention calculation
        mask = self.__create_mask(keys, self.labels_map[PADDING_VALUE])
        summaries, scores = self.attention(queries, keys, values, mask)

        outputs = self.output_mlp(torch.cat([summaries, queries], dim=-1))

        probs = self.inference_softmax(outputs)

        return probs, y_lens

    def __forward_inference(self, keys, values, encoding_lens):
        batch_size = keys.shape[0]
        assert batch_size == 1

        w = next(self.parameters())
        eos = w.new_zeros(1).fill_(self.labels_map[EOS]).type(torch.long)
        y_prev = self.word_piece_embedding(eos)

        # Initialize hidden with a transformation from the last state
        hidden = torch.zeros((batch_size, 1, self.rnn_hidden_dim), dtype=torch.float32)
        outputs = torch.zeros(MAX_LEN, batch_size, self.vocab_dim)
        attentions = torch.zeros(MAX_LEN, batch_size, keys.shape[1])
        mask = self.__create_mask(keys, self.labels_map[PADDING_VALUE])

        if CUDA_ENABLED:
            outputs = outputs.cuda()
            attentions = attentions.cuda()
            hidden = hidden.cuda()

        for t in range(MAX_LEN):
            # query [bs, time, features]
            query, hidden = self.rnn.forward_one_step(y_prev.unsqueeze(1), hidden)
            summaries, score = self.attention(query, keys, values, mask)
            summary = summaries.squeeze(1)
            output = self.output_mlp(torch.cat([summary, query.squeeze(1)], dim=-1))

            # Store results
            outputs[t] = output
            attentions[t] = score

            probs = self.inference_softmax(output)
            best_index = probs.max(1)[1]

            if best_index.item() == self.labels_map[EOS]:
                return outputs[:t].transpose(0, 1), torch.tensor([t], dtype=torch.long), attentions[:t].transpose(0, 1)

            y_prev = self.word_piece_embedding(best_index)

        return outputs.transpose(0, 1), torch.tensor([MAX_LEN], dtype=torch.long), attentions.transpose(0, 1)


@serializable(model=True)
class TDSSeq2Seq(nn.Module):
    encoder_args: Dict[str, str] = field(default_factory=dict)
    decoder_args: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        super().__init__()
        self.encoder = TDSEncoder(**self.encoder_args)
        self.decoder = TDSDecoder(**self.decoder_args)

    def forward(self, xs, xlens, y_labels=None):
        y_in, y_out = list(), list()
        w = next(self.parameters())
        eos = w.new_zeros(1).fill_(self.decoder.labels_map[EOS]).type(torch.int32)

        encoding, encoding_lens = self.encoder(xs, xlens)

        if y_labels is not None:
            for y in y_labels:
                y_in.append(torch.cat([eos, y], dim=0))
                y_out.append(torch.cat([y, eos], dim=0))

            y_lens = [y.size(0) for y in y_in]

            y_in_labels = torch.nn.utils.rnn.pad_sequence(y_in, batch_first=True).type(torch.LongTensor)
            y_out_labels = torch.nn.utils.rnn.pad_sequence(y_out, batch_first=True).type(torch.LongTensor)

            if CUDA_ENABLED:
                y_in_labels = y_in_labels.cuda()
                y_out_labels = y_out_labels.cuda()

            probs, y_lens = self.decoder(encoding, encoding_lens, y_in_labels, y_lens)
            loss = torch_functional.cross_entropy(probs.view((-1, probs.size(2))), y_out_labels.view(-1),
                                                  ignore_index=self.decoder.labels_map[PADDING_VALUE])
            return probs, y_lens, loss
        else:
            # Perform inference only for batch_size=1
            probs, y_lens, attentions = self.decoder(encoding, encoding_lens)
            return probs, y_lens, attentions
