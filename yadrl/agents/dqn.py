import random
from copy import deepcopy
from typing import Any
from typing import NoReturn

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from yadrl.agents.base import BaseOffPolicy
from yadrl.common.scheduler import LinearScheduler
from yadrl.common.utils import huber_loss, quantile_hubber_loss
from yadrl.networks.models import CategoricalDQNModel
from yadrl.networks.models import DQNModel
from yadrl.networks.models import QuantileDQNModel


class DQN(BaseOffPolicy):
    def __init__(self,
                 phi: nn.Module,
                 lrate: float,
                 grad_norm_value: float,
                 adam_eps: float,
                 epsilon_annealing_steps: float,
                 epsilon_min: float,
                 v_min: float = -100.0,
                 v_max: float = 100.0,
                 atoms_dim: int = 51,
                 quantiles_dim: int = 50,
                 noise_type: str = 'none',
                 use_double_q: bool = False,
                 use_dueling: bool = False,
                 distribution_type: str = 'none', **kwargs):
        super(DQN, self).__init__(agent_type='dqn', **kwargs)
        assert distribution_type in ('none', 'categorical', 'quantile')

        self._atoms_dim = atoms_dim

        self._grad_norm_value = grad_norm_value

        self._use_double_q = use_double_q
        self._use_noise = noise_type != 'none'
        self._distribution_type = distribution_type

        self._epsilon_scheduler = LinearScheduler(
            start_value=1.0,
            end_value=epsilon_min,
            annealing_steps=epsilon_annealing_steps)

        if distribution_type == 'categorical':
            self._qv = CategoricalDQNModel(
                phi=phi,
                output_dim=self._action_dim,
                atoms_dim=atoms_dim,
                dueling=use_dueling,
                noise_type=noise_type).to(self._device)
            self._v_limit = (v_min, v_max)
            self._z_delta = (v_max - v_min) / (self._atoms_dim - 1)
            self._atoms = torch.linspace(
                v_min, v_max, atoms_dim, device=self._device).unsqueeze(0)
        elif distribution_type == 'quantile':
            self._qv = QuantileDQNModel(
                phi=phi,
                output_dim=self._action_dim,
                quantiles_dim=quantiles_dim,
                dueling=use_dueling,
                noise_type=noise_type).to(self._device)
            self._cumulative_density = torch.from_numpy(
                (np.arange(quantiles_dim) + 0.5) / quantiles_dim
            ).float().unsqueeze(0).to(self._device)
        else:
            self._qv = DQNModel(
                phi=phi,
                output_dim=self._action_dim,
                dueling=use_dueling,
                noise_type=noise_type).to(self._device)
        self.load()
        self._target_qv = deepcopy(self._qv)

        self._optim = optim.Adam(self._qv.parameters(), lr=lrate, eps=adam_eps)

    def _act(self, state: int, train: bool = False) -> np.ndarray:
        state = torch.from_numpy(state).float().unsqueeze(0).to(self._device)
        state = self._state_normalizer(state, self._device)

        self._qv.eval()
        with torch.no_grad():
            q_value = self._qv(state, train)
            if self._distribution_type == 'categorical':
                q_value = q_value.mul(self._atoms.expand_as(q_value)).sum(-1)
            elif self._distribution_type == 'quantile':
                q_value = q_value.mean(-1)
        self._qv.train()

        eps_flag = random.random() > self._epsilon_scheduler.step()
        if eps_flag or self._use_noise or not train:
            return q_value.argmax(-1)[0].cpu().numpy()
        return random.randint(0, self._action_dim - 1)

    def _update(self):
        batch = self._memory.sample(self._batch_size, self._device)

        if self._distribution_type == 'categorical':
            loss = self._compute_categorical_loss(batch)
        elif self._distribution_type == 'quantile':
            loss = self._compute_quantile_loss(batch)
        else:
            loss = self._compute_td_loss(batch)

        self._writer.add_scalar('loss', loss, self._step)
        self._optim.zero_grad()
        loss.backward()
        if self._grad_norm_value > 0.0:
            nn.utils.clip_grad_norm_(self._qv.parameters(),
                                     self._grad_norm_value)
        self._optim.step()

        self._update_target(self._qv, self._target_qv)

    def _compute_td_loss(self, batch):
        state = self._state_normalizer(batch.state, self._device)
        next_state = self._state_normalizer(batch.next_state, self._device)

        with torch.no_grad():
            target_next_q = self._target_qv(next_state, True)
            if self._use_double_q:
                next_action = self._qv(next_state, True).argmax(1).view(-1, 1)
                target_next_q = target_next_q.gather(1, next_action)
            else:
                target_next_q = target_next_q.max(1)[0].view(-1, 1)

            target_q = self._td_target(batch.reward, batch.mask, target_next_q)
        expected_q = self._qv(state, True).gather(1, batch.action.long())
        loss = huber_loss(expected_q, target_q)

        return loss

    def _compute_categorical_loss(self, batch):
        state = self._state_normalizer(batch.state, self._device)
        next_state = self._state_normalizer(batch.next_state, self._device)

        batch_vec = torch.arange(self._batch_size).long()
        with torch.no_grad():
            next_probs = self._target_qv(next_state, True)
            exp_atoms = self._atoms.expand_as(next_probs)

            if self._use_double_q:
                probs = self._qv(next_state, True)
                next_q = probs.mul(exp_atoms).sum(-1)
            else:
                next_q = next_probs.mul(exp_atoms).sum(-1)
            next_action = next_q.argmax(-1).long()

            next_probs = next_probs[batch_vec, next_action, :]
            target_probs = torch.zeros(next_probs.shape, device=self._device)

            next_atoms = self._td_target(batch.reward, batch.mask, self._atoms)
            next_atoms = torch.clamp(next_atoms, *self._v_limit)

            bj = (next_atoms - self._v_limit[0]) / self._z_delta
            l = bj.floor()
            u = bj.ceil()

            delta_l_prob = next_probs * (u + (u == l).float() - bj)
            delta_u_prob = next_probs * (bj - l)

            for i in range(self._batch_size):
                target_probs[i].index_add_(0, l[i].long(), delta_l_prob[i])
                target_probs[i].index_add_(0, u[i].long(), delta_u_prob[i])

        action = batch.action.squeeze().long()
        probs = self._qv(state, True)[batch_vec, action, :]
        probs = torch.clamp(probs, 1e-7, 1.0)
        loss = -(target_probs * probs.log()).sum(-1)

        return loss.mean()

    def _compute_quantile_loss(self, batch):
        state = self._state_normalizer(batch.state, self._device)
        next_state = self._state_normalizer(batch.next_state, self._device)

        batch_vec = torch.arange(self._batch_size).long()
        with torch.no_grad():
            next_quantiles = self._target_qv(next_state, True)
            if self._use_double_q:
                next_q = self._qv(next_state, True).mean(-1)
            else:
                next_q = next_quantiles.mean(-1)
            next_action = next_q.argmax(-1).long()

            next_quantiles = next_quantiles[batch_vec, next_action, :]
            target_quantiles = self._td_target(batch.reward, batch.mask,
                                               next_quantiles)

        action = batch.action.long().squeeze()
        expected_quantiles = self._qv(state, True)
        expected_quantiles = expected_quantiles[batch_vec, action, :]

        loss = quantile_hubber_loss(
            prediction=expected_quantiles,
            target=target_quantiles,
            cumulative_density=self._cumulative_density)

        return loss

    def load(self) -> NoReturn:
        model = self._checkpoint_manager.load()
        if model:
            self._qv.load_state_dict(model['model'])
            self._step = model['step']
            if 'state_norm' in model:
                self._state_normalizer.load(model['state_norm'])

    def save(self, criterion_value: Any):
        state_dict = dict()
        state_dict['model'] = self._qv.state_dict()
        state_dict['step'] = self._step
        if self._use_state_normalization:
            state_dict['state_norm'] = self._state_normalizer.state_dict()
        self._checkpoint_manager.save(state_dict, self._step, criterion_value)

    @property
    def parameters(self):
        return self._qv.named_parameters()

    @property
    def target_parameters(self):
        return self._target_qv.named_parameters()
