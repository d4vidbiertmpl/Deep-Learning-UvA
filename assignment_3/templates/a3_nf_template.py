import argparse

import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import torch.nn.functional as F
import numpy as np
from datasets.mnist import mnist
import os
from torchvision.utils import save_image


def log_prior(x):
    """
    Compute the elementwise log probability of a standard Gaussian, i.e.
    N(x | mu=0, sigma=1).
    """
    # 1/2 * np.log(2 * np.pi) because D/2 gave me a too large factor
    logp = torch.sum(- 0.5 * np.log(2 * np.pi) - 0.5 * x.pow(2), dim=1)
    return logp


def sample_prior(size):
    """
    Sample from a standard Gaussian.
    """
    sample = torch.randn(size)

    if torch.cuda.is_available():
        sample = sample.cuda()

    return sample


def get_mask():
    mask = np.zeros((28, 28), dtype='float32')
    for i in range(28):
        for j in range(28):
            if (i + j) % 2 == 0:
                mask[i, j] = 1

    mask = mask.reshape(1, 28 * 28)
    mask = torch.from_numpy(mask)

    return mask


class Coupling(torch.nn.Module):
    def __init__(self, c_in, mask, n_hidden=1024):
        super().__init__()
        self.n_hidden = n_hidden
        self.c_in = c_in

        # Assigns mask to self.mask and creates reference for pytorch.
        self.register_buffer('mask', mask)

        # Create shared architecture to generate both the translation and
        # scale variables.
        # Suggestion: Linear ReLU Linear ReLU Linear.
        self.nn = torch.nn.Sequential(
            nn.Linear(c_in, self.n_hidden),
            nn.ReLU(),
            nn.Linear(self.n_hidden, self.n_hidden),
            nn.ReLU(),
            nn.Linear(self.n_hidden, self.c_in * 2)
        )

        # The nn should be initialized such that the weights of the last layer
        # is zero, so that its initial transform is identity.
        self.nn[-1].weight.data.zero_()
        self.nn[-1].bias.data.zero_()

    def forward(self, z, ldj, reverse=False):
        # Implement the forward and inverse for an affine coupling layer. Split
        # the input using the mask in self.mask. Transform one part with
        # Make sure to account for the log Jacobian determinant (ldj).
        # For reference, check: Density estimation using RealNVP.
        h, t = self.nn(self.mask * z).split(self.c_in, dim=1)

        # NOTE: For stability, it is advised to model the scale via:
        # log_scale = tanh(h), where h is the scale-output
        # from the NN.
        log_scale = torch.tanh(h)

        if not reverse:
            # According eq. 9 in the RealNVP paper
            z = self.mask * z + (1 - self.mask) * (z * torch.exp(log_scale) + t)
            ldj += torch.sum((1 - self.mask) * log_scale, dim=1)
        else:
            # According eq. 8 in the RealNVP paper
            z = self.mask * z + (1 - self.mask) * (z - t) * torch.exp(-log_scale)

        return z, ldj


class Flow(nn.Module):
    def __init__(self, shape, n_flows=4):
        super().__init__()
        channels, = shape

        mask = get_mask()

        self.layers = torch.nn.ModuleList()

        for i in range(n_flows):
            self.layers.append(Coupling(c_in=channels, mask=mask))
            self.layers.append(Coupling(c_in=channels, mask=1 - mask))

        self.z_shape = (channels,)

    def forward(self, z, logdet, reverse=False):
        if not reverse:
            for layer in self.layers:
                z, logdet = layer(z, logdet)
        else:
            for layer in reversed(self.layers):
                z, logdet = layer(z, logdet, reverse=True)

        return z, logdet


class Model(nn.Module):
    def __init__(self, shape):
        super().__init__()
        self.flow = Flow(shape)
        self.shape = shape

    def dequantize(self, z):
        return z + torch.rand_like(z)

    def logit_normalize(self, z, logdet, reverse=False):
        """
        Inverse sigmoid normalization.
        """
        alpha = 1e-5

        if not reverse:
            # Divide by 256 and update ldj.
            z = z / 256.
            logdet -= np.log(256) * np.prod(z.size()[1:])

            # Logit normalize
            z = z * (1 - alpha) + alpha * 0.5
            logdet += torch.sum(-torch.log(z) - torch.log(1 - z), dim=1)
            z = torch.log(z) - torch.log(1 - z)

        else:
            # Inverse normalize
            z = torch.sigmoid(z)
            logdet += torch.sum(torch.log(z) + torch.log(1 - z), dim=1)
            z = (z - alpha * 0.5) / (1 - alpha)

            # Multiply by 256.
            logdet += np.log(256) * np.prod(z.size()[1:])
            z = z * 256.

        return z, logdet

    def forward(self, input):
        """
        Given input, encode the input to z space. Also keep track of ldj.
        """
        z = input
        ldj = torch.zeros(z.size(0), device=z.device)

        z = self.dequantize(z)  # discrete => continuous image
        z, ldj = self.logit_normalize(z, ldj)  #
        z, ldj = self.flow(z, ldj)

        # Compute log_pz and log_px per example
        log_pz = log_prior(z)
        log_px = log_pz + ldj

        return log_px

    def sample(self, n_samples):
        """
        Sample n_samples from the model. Sample from prior and create ldj.
        Then invert the flow and invert the logit_normalize.
        """

        z = sample_prior((n_samples,) + self.flow.z_shape)
        ldj = torch.zeros(z.size(0), device=z.device)

        # Invert the flow and invert the logit_normalize.
        x_hat, ldj = self.flow(z, ldj, reverse=True)
        x_hat, ldj = self.logit_normalize(x_hat, ldj, reverse=True)

        return x_hat


def epoch_iter(model, data, optimizer, device):
    """
    Perform a single epoch for either the training or validation.
    use model.training to determine if in 'training mode' or not.

    Returns the average bpd ("bits per dimension" which is the negative
    log_2 likelihood per dimension) averaged over the complete epoch.
    """

    nlls = []
    for step, (batch_inputs, _) in enumerate(data):
        log_px = model(batch_inputs.view(-1, model.shape[0]).to(device))
        nll = -torch.mean(log_px)  # negative log-likelihood
        nlls.append(nll.item())

        if model.training:
            optimizer.zero_grad()
            nll.backward()
            # Use gradient clipping as discussed on piazza
            torch.nn.utils.clip_grad_norm(model.parameters(), max_norm=ARGS.max_norm)
            optimizer.step()

    # Change of base from log_e to log_2
    avg_bpd = np.mean(np.asarray(nlls) / model.shape[0] / np.log(2))

    return avg_bpd


def run_epoch(model, data, optimizer, device):
    """
    Run a train and validation epoch and return average bpd for each.
    """
    traindata, valdata = data

    model.train()
    train_bpd = epoch_iter(model, traindata, optimizer, device)

    model.eval()
    val_bpd = epoch_iter(model, valdata, optimizer, device)

    return train_bpd, val_bpd


def plot_grid(model, n_samples, epoch):
    x_hats = model.sample(n_samples)
    save_image(x_hats.view(n_samples, 1, 28, 28), 'images_nfs/norm_flow_sample_{}.png'.format(epoch),
               nrow=int(np.sqrt(n_samples)), normalize=True)


def save_bpd_plot(train_curve, val_curve, filename):
    plt.figure(figsize=(12, 6))
    plt.plot(train_curve, label='train bpd')
    plt.plot(val_curve, label='validation bpd')
    plt.legend()
    plt.xlabel('epochs')
    plt.ylabel('bpd')
    plt.tight_layout()
    plt.savefig(filename)


def main():
    data = mnist()[:2]  # ignore test split
    device = ARGS.device
    n_samples = ARGS.n_samples

    model = Model(shape=[784]).to(device)

    if torch.cuda.is_available():
        model = model.cuda()

    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

    os.makedirs('images_nfs', exist_ok=True)

    plot_grid(model, n_samples, 0)

    train_curve, val_curve = [], []
    for epoch in range(ARGS.epochs):
        bpds = run_epoch(model, data, optimizer, device)
        train_bpd, val_bpd = bpds
        train_curve.append(train_bpd)
        val_curve.append(val_bpd)
        print("[Epoch {epoch}] train bpd: {train_bpd} val_bpd: {val_bpd}".format(
            epoch=epoch, train_bpd=train_bpd, val_bpd=val_bpd))

        # --------------------------------------------------------------------
        #  Add functionality to plot samples from model during training.
        #  You can use the make_grid functionality that is already imported.
        #  Save grid to images_nfs/
        # --------------------------------------------------------------------

        # Same procedure I already implemented in the VAE file
        if epoch % 5 == 0 or epoch == ARGS.epochs - 1:
            plot_grid(model, n_samples, epoch + 1)

    save_bpd_plot(train_curve, val_curve, 'nfs_bpd.png')
    torch.save(model, "trained_norm_flow.pth")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', default=40, type=int,
                        help='max number of epochs')
    parser.add_argument('--device', type=str, default="cpu", help="Training device 'cpu' or 'cuda:0'")
    parser.add_argument('--max_norm', type=float, default=10.0)
    parser.add_argument('--n_samples', default=36, type=int, help='number of samples')

    ARGS = parser.parse_args()

    main()
