import torch

USE_CUDA = True
CUDA_ENABLED = USE_CUDA and torch.cuda.is_available()
DEVICE = torch.device("cuda" if CUDA_ENABLED else "cpu")
