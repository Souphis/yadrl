import numpy as np
import torch

import yadrl.common.ops as ops
from yadrl.agents.dpg.ddpg import DDPG
from yadrl.common.memory import Batch


class QuantileDDPG(DDPG, agent_type='quantile_regression_ddpg'):
    def __init__(self,
                 support_dim: int = 100,
                 **kwargs):
        self._support_dim = support_dim
        super().__init__(**kwargs)
        self._cumulative_density = torch.from_numpy(
            (np.arange(support_dim) + 0.5) / support_dim
        ).float().unsqueeze(0).to(self._device)

    def _sample_q(self,
                  state: torch.Tensor,
                  action: torch.Tensor,
                  sample_noise: bool = False) -> torch.Tensor:
        return super()._sample_q(state, action, sample_noise).mean(-1)

    def _compute_critic_loss(self, batch: Batch) -> torch.Tensor:
        with torch.no_grad():
            next_action = self.target_pi(batch.next_state)
            self.target_qv.sample_noise()
            next_quantiles = self.target_qv(batch.next_state, next_action)
            target_quantiles = ops.td_target(
                batch.reward, batch.mask, next_quantiles,
                batch.discount_factor * self._discount)

        self.qv.sample_noise()
        expected_quantiles = self.qv(batch.state, batch.action)
        loss = ops.quantile_hubber_loss(expected_quantiles, target_quantiles,
                                        self._cumulative_density)
        return loss
