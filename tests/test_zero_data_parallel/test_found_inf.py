from functools import partial

import colossalai
from colossalai.utils.cuda import get_current_device
import pytest
import torch
import torch.multiprocessing as mp
from colossalai.nn.optimizer import HybridAdam
from colossalai.testing import parameterize, rerun_on_exception
from colossalai.utils import free_port
from colossalai.zero.init_ctx import ZeroInitContext
from colossalai.zero.shard_utils import BucketTensorShardStrategy
from colossalai.zero.sharded_model import ShardedModelV2
from colossalai.zero.sharded_optim import ShardedOptimizerV2
from colossalai.zero.sharded_optim._utils import has_inf_or_nan
from tests.components_to_test.registry import non_distributed_component_funcs
from tests.test_zero_data_parallel.test_sharded_optim_v2 import _run_step

from common import CONFIG


@parameterize("cpu_offload", [True, False])
@parameterize("shard_strategy_class", [BucketTensorShardStrategy])
@parameterize("gpu_margin_mem_ratio", [0.0, 0.7])
def _run_test_found_inf(cpu_offload, shard_strategy_class, gpu_margin_mem_ratio):
    test_models = ['repeated_computed_layers']
    shard_strategy = shard_strategy_class()

    for model_name in test_models:
        get_components_func = non_distributed_component_funcs.get_callable(model_name)
        model_builder, train_dataloader, _, optimizer_class, criterion = get_components_func()

        with ZeroInitContext(target_device=torch.device(f'cpu:0') if cpu_offload else get_current_device(),
                             shard_strategy=shard_strategy,
                             shard_param=True):
            zero_model = model_builder(checkpoint=True)
        zero_model = ShardedModelV2(
            zero_model,
            shard_strategy,
            offload_config=dict(device='cpu') if cpu_offload else None,
            use_memory_tracer=gpu_margin_mem_ratio > 0.0,
            reuse_fp16_shard=True,
        )

        sharded_optim = HybridAdam(zero_model.parameters(), lr=1e-3)
        sharded_optim = ShardedOptimizerV2(zero_model,
                                           sharded_optim,
                                           cpu_offload=cpu_offload,
                                           gpu_margin_mem_ratio=gpu_margin_mem_ratio)

        for i, (data, label) in enumerate(train_dataloader):
            if i > 1:
                break
            assert zero_model.overflow_counter == 0
            data, label = data.cuda(), label.cuda()
            _run_step(zero_model, sharded_optim, data, label, criterion, False)
            for param in zero_model.parameters():
                assert not has_inf_or_nan(param.colo_attr.sharded_data_tensor.payload)


def _run_dist(rank, world_size, port):
    colossalai.launch(config=CONFIG, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    _run_test_found_inf()


# use_cpuadam = True can be used with cpu_offload = False
@pytest.mark.dist
@pytest.mark.parametrize("world_size", [1, 2])
@rerun_on_exception(exception_type=mp.ProcessRaisedException, pattern=".*Address already in use.*")
def test_found_inf(world_size):
    run_func = partial(_run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_found_inf(world_size=2)
