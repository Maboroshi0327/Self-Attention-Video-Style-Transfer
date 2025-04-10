import torch
import torch.nn as nn
from torch import linalg as LA
from torch.nn import functional as F

import numpy as np

from vit import ViT_MultiScale, ViT_torch


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
    def __init__(self, multi_scale=True):
        super().__init__()
        self.conv1 = nn.Sequential(
            ConvReLU(512, 256, 3, 1) if multi_scale else ConvReluInterpolate(512, 256, 3, 1, 2),
            ConvReLU(256, 256, 3, 1),
            ConvReLU(256, 256, 3, 1),
            ConvReLU(256, 256, 3, 1),
            ConvReluInterpolate(256, 128, 3, 1, 2),
        )

        self.conv2 = nn.Sequential(
            ConvReLU(128, 128, 3, 1),
            ConvReluInterpolate(128, 64, 3, 1, 2),
        )

        self.conv3 = nn.Sequential(
            ConvReLU(64, 64, 3, 1),
            ConvReLU(64, 3, 3, 1),
        )

    def forward(self, fcs: torch.Tensor):
        fcs = self.conv1(fcs)
        fcs = self.conv2(fcs)
        cs = self.conv3(fcs)
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
    def __init__(self, qkv_dim, activation="softmax"):
        super().__init__()
        self.f = nn.Conv2d(qkv_dim, qkv_dim, 1)
        self.g = nn.Conv2d(qkv_dim, qkv_dim, 1)
        self.h = nn.Conv2d(qkv_dim, qkv_dim, 1)
        self.norm_q = nn.InstanceNorm2d(qkv_dim, affine=False)
        self.norm_k = nn.InstanceNorm2d(qkv_dim, affine=False)
        self.norm_v = nn.InstanceNorm2d(qkv_dim, affine=False)

        if activation == "softmax":
            self.activation = Softmax()
        elif activation == "cosine":
            self.activation = CosineSimilarity()
        else:
            raise ValueError(f"Unknown activation function: {activation}")

    def forward(self, fc: torch.Tensor, fs: torch.Tensor, fcs: torch.Tensor):
        # Q^T
        Q = self.f(self.norm_q(fc))
        b, _, h, w = Q.size()
        Q = Q.view(b, -1, h * w).permute(0, 2, 1)

        # K
        K = self.g(self.norm_k(fs))
        b, _, h, w = K.size()
        K = K.view(b, -1, h * w)

        # V^T
        V = self.h(fs)
        b, _, h, w = V.size()
        V = V.view(b, -1, h * w).permute(0, 2, 1)

        # A * V^T
        A = self.activation(Q, K)
        M = torch.bmm(A, V)

        # S
        Var = torch.bmm(A, V**2) - M**2
        S = torch.sqrt(Var.clamp(min=1e-6))

        # Reshape M and S
        b, _, h_c, w_c = fc.size()
        M = M.view(b, h_c, w_c, -1).permute(0, 3, 1, 2)
        S = S.view(b, h_c, w_c, -1).permute(0, 3, 1, 2)

        if M.size() != fcs.size():
            _, _, h_cs, w_cs = fcs.size()
            M = F.interpolate(M, size=(h_cs, w_cs), mode="bilinear", align_corners=False)
            S = F.interpolate(S, size=(h_cs, w_cs), mode="bilinear", align_corners=False)

        return S * self.norm_v(fcs) + M


class AdaViT(nn.Module):
    def __init__(self, activation="softmax"):
        super().__init__()
        self.adaattn1 = AdaAttN(qkv_dim=512, activation=activation)
        self.adaattn2 = AdaAttN(qkv_dim=512, activation=activation)
        self.adaattn3 = AdaAttN(qkv_dim=512, activation=activation)

        self.decoder = Decoder(multi_scale=False)

    def forward(self, fc, fs):
        fcs = self.adaattn1(fc[0], fs[0], fc[0])
        fcs = self.adaattn2(fc[1], fs[1], fcs)
        fcs = self.adaattn3(fc[2], fs[2], fcs)
        cs = self.decoder(fcs)
        return cs


class AdaMSViT(nn.Module):
    def __init__(self, activation="softmax"):
        super().__init__()
        self.adaattn1 = AdaAttN(qkv_dim=512, activation=activation)
        self.adaattn2 = AdaAttN(qkv_dim=512, activation=activation)
        self.adaattn3 = AdaAttN(qkv_dim=512, activation=activation)

        self.decoder = Decoder(multi_scale=True)

    def forward(self, fc, fs):
        fcs = self.adaattn1(fc[0], fs[0], fc[0])
        fcs = self.adaattn2(fc[1], fs[1], fcs)
        fcs = self.adaattn3(fc[2], fs[2], fcs)

        cs = self.decoder(fcs)
        return cs


def test_AdaViT():
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # Load a random tensor and normalize it
    c = torch.rand(8, 3, 256, 256).to(device)
    s = torch.rand(8, 3, 256, 256).to(device)

    # Create a StylizingNetwork model and forward propagate the input tensor
    vit_c = ViT_torch(pos_embedding=True).to(device)
    vit_s = ViT_torch(pos_embedding=False).to(device)
    model = AdaViT().to(device)

    # Forward pass
    fc = vit_c(c)
    fs = vit_s(s)
    cs = model(fc, fs)
    print(c.shape)
    print(cs.shape)


def test_AdaMSViT():
    device = "cuda" if torch.cuda.is_available() else "cpu"

    image_size = (256, 512)
    activation = "softmax"

    # Load a random tensor and normalize it
    c = torch.rand(8, 3, image_size[0], image_size[1]).to(device)
    s = torch.rand(8, 3, image_size[0], image_size[1]).to(device)

    # Create a StylizingNetwork model and forward propagate the input tensor
    vit_c = ViT_MultiScale(image_size=image_size, pos_embedding=True).to(device)
    vit_s = ViT_MultiScale(image_size=image_size, pos_embedding=False).to(device)
    model = AdaMSViT(activation=activation).to(device)

    # Forward pass
    fc = vit_c(c)
    fs = vit_s(s)
    cs = model(fc, fs)
    print(c.shape)
    print(cs.shape)


if __name__ == "__main__":
    test_AdaMSViT()
