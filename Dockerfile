FROM pytorch/pytorch:1.1.0-cuda10.0-cudnn7.5-devel
ARG CUDA=false
ENV LANG C.UTF-8

WORKDIR /workspace/

COPY . .

# copy and make executable the entrypoint script
COPY scripts/starter.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/starter.sh

# install basics
RUN apt-get update -y \
    && apt-get install -y git curl ca-certificates bzip2 cmake tree htop bmon iotop sox libsox-dev libsox-fmt-all vim wget ffmpeg

ENV LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH

# install python deps
RUN pip install -r requirements.txt \
    && pip install -r post_requirements.txt

# install ctcdecode
RUN git clone --recursive https://github.com/parlance/ctcdecode.git \
    && cd ctcdecode; pip install .

RUN pip install .

ENTRYPOINT ["starter.sh"]
