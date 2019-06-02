from typing import Callable, Sequence, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.distributions.multivariate_normal import MultivariateNormal
from torch.distributions.categorical import Categorical


class ValueHead(nn.Module):
    def __init__(self, input_dim: int, ddpg_init: bool = False):
        super(ValueHead, self).__init__()
        self._value = nn.Linear(input_dim, 1)

        if ddpg_init:
            self._initialize_variables()

    def _initialize_variables(self):
        self._value.weight.data.uniform_(-3e-3, 3e-3)
        self._value.bias.data.uniform(-3e-3, 3e-3)

    def forward(self, x: torch.Tensor):
        return self._value(x)


class DeterministicPolicyHead(nn.Module):
    def __init__(self,
                 input_dim: int,
                 output_dim: int,
                 ddpg_init: bool = False,
                 activation_fn: Callable = torch.tanh):
        super(DeterministicPolicyHead, self).__init__()
        self._activation_fn = activation_fn

        self._action = nn.Linear(input_dim, output_dim)

        if ddpg_init:
            self._initialize_variables()

    def _initialize_variables(self):
        self._action.weight.data.uniform_(-3e-3, 3e-3)
        self._action.bias.data.uniform(-3e-3, 3e-3)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self._activation_fn:
            return self._activation_fn(self._action(x))
        return self._action(x)


class GaussianPolicyHead(nn.Module):
    def __init__(self,
                 input_dim: int,
                 output_dim: int,
                 independent_std: bool = True,
                 squash: bool = False,
                 std_limits: Sequence[float] = (-20.0, 2.0)):
        super(GaussianPolicyHead, self).__init__()
        self._independend_std = independent_std
        self._squash = squash
        self._std_limits = std_limits

        self._mean = nn.Linear(input_dim, output_dim)
        if independent_std:
            self._log_std = nn.Parameter(torch.zeros(1, output_dim))
        else:
            self._log_std = nn.Linear(input_dim, output_dim)
        self._initialize_parameters()

    def _initialize_parameters(self):
        if self._independend_std:
            self._log_std.weight.data.uniform_(-3e-3, 3e-3)
            self._log_std.bias.data.uniform_(-3e-3, 3e-3)
        self._mean.weight.data.uniform_(-3e-3, 3e-3)
        self._mean.bias.data.uniform_(-3e-3, 3e-3)

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        mean = self._mean(x)
        log_std = self._log_std.expand_as(mean) if self._independend_std else self._log_std(x)
        log_std = torch.clamp(log_std, *self._std_limits)
        return mean, log_std

    def sample(self,
               x: torch.Tensor,
               raw_action: Optional[torch.Tensor] = None,
               reparameterize: bool = True) -> Tuple[torch.Tensor, ...]:
        mean, log_std = self.forward(x)
        covariance = torch.diag_embed(torch.exp(log_std))
        dist = MultivariateNormal(loc=mean, scale_tril=covariance)

        if not raw_action:
            raw_action = dist.rsample() if reparameterize else dist.sample()

        action = torch.tanh(raw_action) if self._squash else raw_action
        log_prob = dist.log_prob(raw_action).unsqueeze(-1)
        if self._squash:
            log_prob -= self._squash_correction(raw_action)
        entropy = dist.entropy().unsqueeze(-1)

        return action, log_prob, entropy, torch.tanh(dist.mean)

    @staticmethod
    def _squash_correction(action: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
        return torch.log(1.0 - torch.tanh(action).pow(2) + eps).sum(-1, keepdim=True)


class CategoricalPolicyHead(nn.Module):
    def __init__(self, input_dim: int, output_dim: int):
        super(CategoricalPolicyHead, self).__init__()
        self._logits = nn.Linear(input_dim, output_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.softmax(x, dim=1)
        return x

    def sample(self,
               x: torch.Tensor,
               action: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor, ...]:
        x = self.forward(x)
        dist = Categorical(x)

        if not action:
            action = dist.sample()
        log_prob = dist.log_prob(action)
        entropy = dist.entropy()
        return action, log_prob, entropy, x.argmax(-1, keepdim=True)
