import torch
import torch.nn as nn
from torch import linalg as LA
from torch.nn import functional as F

import numpy as np

from vit import ViT


class Conv(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int):
        super().__init__()
        pad = int(np.floor(kernel_size / 2))
        self.pad = torch.nn.ReflectionPad2d(pad)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride)

    def forward(self, x):
        x = self.pad(x)
        x = self.conv(x)
        return x


class ConvReLU(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int):
        super().__init__()
        self.conv = Conv(in_channels, out_channels, kernel_size, stride)
        self.relu = nn.ReLU()

    def forward(self, x):
        x = self.conv(x)
        x = self.relu(x)
        return x


class ConvTanh(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int):
        super().__init__()
        self.conv = Conv(in_channels, out_channels, kernel_size, stride)
        self.tanh = nn.Tanh()

    def forward(self, x):
        x = self.conv(x)
        x = self.tanh(x)
        x = (x + 1) / 2 * 255
        return x


class ConvReluInterpolate(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, stride: int, scale_factor: float):
        super().__init__()
        self.conv = Conv(in_channels, out_channels, kernel_size, stride)
        self.relu = nn.ReLU()
        self.scale_factor = scale_factor

    def forward(self, x):
        x = self.conv(x)
        x = self.relu(x)
        x = F.interpolate(x, scale_factor=self.scale_factor, mode="bilinear", align_corners=False)
        return x


class Decoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = ConvReluInterpolate(512, 256, 3, 1, 2)

        self.conv2 = nn.Sequential(
            ConvReLU(256, 256, 3, 1),
            ConvReLU(256, 256, 3, 1),
            ConvReLU(256, 256, 3, 1),
            ConvReluInterpolate(256, 128, 3, 1, 2),
        )

        self.conv3 = nn.Sequential(
            ConvReLU(128, 128, 3, 1),
            ConvReluInterpolate(128, 64, 3, 1, 2),
        )

        self.conv4 = nn.Sequential(
            ConvReLU(64, 64, 3, 1),
            ConvReLU(64, 3, 3, 1),
        )

    def forward(self, fcs):
        fcs = self.conv1(fcs)
        fcs = self.conv2(fcs)
        fcs = self.conv3(fcs)
        cs = self.conv4(fcs)
        return cs


class Softmax(nn.Module):
    def __init__(self):
        super().__init__()
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, q, k):
        return self.softmax(torch.bmm(q, k))


class CosineSimilarity(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, q, k):
        """
        q:   (b, t, d)
        k:   (b, d, t)
        out: (b, t, t)
        """
        q_norm = LA.vector_norm(q, dim=-1, keepdim=True)
        k_norm = LA.vector_norm(k, dim=1, keepdim=True)
        s = torch.bmm(q, k) / torch.bmm(q_norm, k_norm) + 1
        a = s / s.sum(dim=-1, keepdim=True)
        return a


class AdaAttN(nn.Module):
    def __init__(self, token_length, qkv_dim, activation="softmax"):
        super().__init__()
        self.f = nn.Linear(qkv_dim, qkv_dim)
        self.g = nn.Linear(qkv_dim, qkv_dim)
        self.h = nn.Linear(qkv_dim, qkv_dim)
        self.norm_q = nn.InstanceNorm1d(token_length, affine=False)
        self.norm_k = nn.InstanceNorm1d(token_length, affine=False)
        self.norm_v = nn.InstanceNorm1d(token_length, affine=False)

        if activation == "softmax":
            self.activation = Softmax()
        elif activation == "cosine":
            self.activation = CosineSimilarity()
        else:
            raise ValueError(f"Unknown activation function: {activation}")

    def forward(self, fc, fs, fcs):
        # Q (b, t, d)
        Q = self.f(self.norm_q(fc))

        # K (b, t, d) -> (b, d, t)
        K = self.g(self.norm_k(fs))
        K = K.permute(0, 2, 1)

        # V (b, t, d)
        V = self.h(fs)

        # A (b, t, t)
        A = self.activation(Q, K)

        # M = A * V -> (b, t, d)
        # S (b, t, d)
        M = torch.bmm(A, V)
        Var = torch.bmm(A, V**2) - M**2
        S = torch.sqrt(Var.clamp(min=1e-6))

        return S * self.norm_v(fcs) + M


class StylizingNetwork(torch.nn.Module):
    def __init__(self, enc_layer_num: int = 3, activation="softmax"):
        super().__init__()
        self.enc_layer_num = enc_layer_num
        self.vit_c = ViT(num_layers=enc_layer_num, pos_embedding=True)
        self.vit_s = ViT(num_layers=enc_layer_num, pos_embedding=False)

        self.adaattn = nn.ModuleList()
        for i in range(enc_layer_num):
            self.adaattn.append(AdaAttN(token_length=1024, qkv_dim=512, activation=activation))

        self.decoder = Decoder()

    def forward(self, c, s):
        fc = self.vit_c(c)
        fs = self.vit_s(s)

        fcs = self.adaattn[0](fc[0], fs[0], fc[0])
        for i in range(1, self.enc_layer_num):
            fcs = self.adaattn[i](fc[i], fs[i], fcs)

        fcs = fcs.permute(0, 2, 1).reshape(-1, 512, 32, 32)
        cs = self.decoder(fcs)
        return cs


if __name__ == "__main__":
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # Load a random tensor and normalize it
    c = torch.rand(4, 3, 256, 256).to(device)
    s = torch.rand(4, 3, 256, 256).to(device)

    # Create a StylizingNetwork model and forward propagate the input tensor
    model = StylizingNetwork().to(device)
    cs = model(c, s)
    print(c.shape)
    print(cs.shape)
