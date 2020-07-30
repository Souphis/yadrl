from typing import NoReturn
from typing import Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

import yadrl.common.utils as utils
from yadrl.agents.base import BaseOffPolicy
from yadrl.common.exploration_noise import GaussianNoise
from yadrl.common.memory import Batch
from yadrl.networks.heads import DeterministicPolicyHead
from yadrl.networks.heads import MultiValueHead


class TD3(BaseOffPolicy):
    def __init__(self,
                 pi_phi: nn.Module,
                 qv_phi: nn.Module,
                 policy_update_frequency: int,
                 pi_lrate: float,
                 qvs_lrate: float,
                 pi_grad_norm_value: float = 0.0,
                 qvs_grad_norm_value: float = 0.0,
                 action_limit: Tuple[float, float] = (-1.0, 1.0),
                 target_noise_limit: Tuple[float, float] = (-0.5, 0.5),
                 noise_std: float = 0.1,
                 target_noise_std: float = 0.2,
                 **kwargs):
        super(TD3, self).__init__(**kwargs)
        GaussianNoise.TORCH_BACKEND = True
        if np.shape(action_limit) != (2,):
            raise ValueError
        self._action_limit = action_limit
        self._target_noise_limit = target_noise_limit

        self._policy_update_frequency = policy_update_frequency

        self._pi_grad_norm_value = pi_grad_norm_value
        self._qvs_grad_norm_value = qvs_grad_norm_value

        self._pi = DeterministicPolicyHead(
            pi_phi, self._action_dim).to(self._device)
        self._target_pi = DeterministicPolicyHead(
            pi_phi, self._action_dim).to(self._device)
        self._target_pi.load_state_dict(self._pi.state_dict())
        self._target_pi.eval()

        self._pi_optim = optim.Adam(self._pi.parameters(), pi_lrate)

        self._qv = MultiValueHead(qv_phi, heads_num=2).to(self._device)
        self._target_qv = MultiValueHead(qv_phi, heads_num=2).to(self._device)
        self._target_qv.load_state_dict(self._qv.state_dict())
        self._target_qv.eval()
        self._qv_optim = optim.Adam(self._qv.parameters(), qvs_lrate)

        self._noise = GaussianNoise(self._action_dim, sigma=noise_std)
        self._target_noise = GaussianNoise(
            self._action_dim, sigma=target_noise_std)

    def _act(self, state, train):
        state = torch.from_numpy(state).float().unsqueeze(0).to(self._device)
        self._pi.eval()
        with torch.no_grad():
            action = self._pi(state)
        self._pi.train()

        if train:
            noise = self._noise().to(self._device)
            action = torch.clamp(action + noise, *self._action_limit)
        return action[0].cpu().numpy()

    def _update(self):
        batch = self._memory.sample(self._batch_size)
        self._update_critic(batch)

        if self._env_step % (self._policy_update_frequency *
                             self._update_frequency) == 0:
            self._update_actor(batch)
            self._update_target(self._pi, self._target_pi)
            self._update_target(self._qv, self._target_qv)

    def _update_critic(self, batch: Batch):
        state = self._state_normalizer(batch.state)
        next_state = self._state_normalizer(batch.next_state)

        noise = self._target_noise().clamp(
            *self._target_noise_limit).to(self._device)
        next_action = self._target_pi(next_state) + noise
        next_action = next_action.clamp(*self._action_limit)

        target_next_qs = self._target_qv(next_state, next_action)
        target_next_q = torch.min(*target_next_qs).view(-1, 1)
        target_q = utils.td_target(
            reward=batch.reward,
            mask=batch.mask,
            target=target_next_q,
            discount=batch.discount_factor * self._discount).detach()
        expected_q1, expected_q2 = self._qv(state, batch.action)

        loss = utils.mse_loss(expected_q1, target_q) + \
               utils.mse_loss(expected_q2, target_q)

        self._qv_optim.zero_grad()
        loss.backward()
        if self._qvs_grad_norm_value > 0.0:
            nn.utils.clip_grad_norm_(self._qv.q1_parameters(),
                                     self._qvs_grad_norm_value)
        self._qv_optim.step()

    def _update_actor(self, batch: Batch):
        actions = self._pi(batch.state)
        loss = -self._qv(batch.state, actions)[0].mean()
        self._pi_optim.zero_grad()
        loss.backward()
        if self._pi_grad_norm_value > 0.0:
            nn.utils.clip_grad_norm_(self._pi.parameters(),
                                     self._pi_grad_norm_value)
        self._pi_optim.step()

    def load(self, path: str) -> NoReturn:
        model = torch.load(str)
        if model:
            self._pi.load_state_dict(model['actor'])
            self._qv.load_state_dict(model['critic'])
            self._target_qv.load_state_dict(model['target_critic'])
            self._env_step = model['step']
            if 'state_norm' in model:
                self._state_normalizer.load(model['state_norm'])

    def save(self):
        state_dict = dict()
        state_dict['actor'] = self._pi.state_dict(),
        state_dict['critic'] = self._qv.state_dict()
        state_dict['target_critic'] = self._target_qv.state_dict()
        state_dict['step'] = self._env_step
        if self._use_state_normalization:
            state_dict['state_norm'] = self._state_normalizer.state_dict()
        torch.save(state_dict, 'model_{}.sth'.format(self._env_step))

    @property
    def parameters(self):
        return list(self._qv.named_parameters()) + \
               list(self._pi.named_parameters())

    @property
    def target_parameters(self):
        return list(self._target_qv.named_parameters()) + \
               list(self._target_pi.named_parameters())
