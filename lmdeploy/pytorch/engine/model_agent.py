# Copyright (c) OpenMMLab. All rights reserved.

import asyncio
import atexit
import os
import threading
from datetime import timedelta
from typing import Any, Callable, Dict, List

import torch
import torch.distributed as dist
from torch import multiprocessing as mp

from lmdeploy.utils import get_logger

from ..backends import get_backend
from ..config import BackendConfig, CacheConfig, ModelConfig
from ..devices import DeviceContext, get_device_manager
from ..distributed import DistContext, get_dist_manager, get_world_rank
from ..model_inputs import ModelInputs
from ..models.patch import add_adapters, build_patched_model, update_custom_module_map
from ..utils import get_gpu_memory
from ..weight_loader.model_weight_loader import load_model_weights
from .cache_engine import CacheEngine

logger = get_logger('lmdeploy')


def _update_cache_config(model_config: ModelConfig,
                         cache_config: CacheConfig,
                         gpu_id: int = 0,
                         host_mem_size: int = 1 * (1 << 30),
                         world_size: int = 1):
    """Update the gpu mem and cpu mem according to model info.

    Args:
        model_config (ModelConfig): The config of the model.
        cache_config (CacheConfig): The config of the cache info.
        gpu_id (int): The GPU id to use.
    """

    def __get_runtime_size(num_free_gpu_mem: int, cache_block_size: int, vocal_size: int):
        """find best prefill num."""
        cache_max_entry_count = cache_config.cache_max_entry_count
        max_prefill_token_num = cache_config.max_prefill_token_num
        runtime_cache_size = 0
        while max_prefill_token_num > 0:
            # lm_head output(2) + to float(4) + estimated misc(1) = 7
            runtime_cache_size = int(max_prefill_token_num * vocal_size * 7)
            num_available = (num_free_gpu_mem - runtime_cache_size) * cache_max_entry_count
            if int(num_available) // cache_block_size >= 16:
                break
            max_prefill_token_num = max_prefill_token_num // 2
        return runtime_cache_size, max_prefill_token_num

    def __get_free_gpu_mem_size(cache_block_size: int):
        """get free gpu memory size."""
        torch.cuda.empty_cache()
        gpu_mem_physical_free, _ = get_gpu_memory(gpu_id)
        logger.debug(f'device<{gpu_id}> free gpu memory:'
                     f' {gpu_mem_physical_free>>20} mb')
        vocal_size = model_config.vocab_size

        runtime_cache_size, max_prefill_token_num = __get_runtime_size(gpu_mem_physical_free, cache_block_size,
                                                                       vocal_size)
        if cache_config.max_prefill_token_num != max_prefill_token_num:
            if max_prefill_token_num <= 0:
                raise RuntimeError('No enough gpu memory for runtime.')
            cache_config.max_prefill_token_num = max_prefill_token_num
            logger.warning(f'device<{gpu_id}> No enough memory. '
                           'update max_prefill_token_num='
                           f'{max_prefill_token_num}')
        gpu_mem_physical_free -= runtime_cache_size
        logger.debug('estimated max runtime memory:'
                     f' {runtime_cache_size>>20} mb')
        return gpu_mem_physical_free * cache_config.cache_max_entry_count

    def __adjust_block_size():
        """adjust block_size."""
        # TODO: support kernel with both large head dim and large block size.
        if model_config.k_head_dim >= 512 and cache_config.block_size > 32:
            cache_config.block_size = 32
            _, rank = get_world_rank()
            if rank == 0:
                logger.warning(f'Update `block_size={cache_config.block_size}`'
                               f' for large `head_dim={model_config.k_head_dim}`.')

    __adjust_block_size()

    cache_block_size = CacheEngine.get_cache_block_size(cache_config.block_size, model_config, world_size,
                                                        cache_config.quant_policy)
    gpu_mem = __get_free_gpu_mem_size(cache_block_size)
    cpu_mem = host_mem_size
    if cache_config.num_cpu_blocks == 0:
        cache_config.num_cpu_blocks = int(cpu_mem / cache_block_size)
        if cache_config.num_cpu_blocks <= 0:
            raise RuntimeError('No enough host memory for kv cache.')
    if cache_config.num_gpu_blocks == 0:
        cache_config.num_gpu_blocks = int(gpu_mem / cache_block_size)
        if cache_config.num_gpu_blocks <= 0:
            raise RuntimeError('No enough gpu memory for kv cache.')
    cache_config.window_size = model_config.sliding_window

    logger.debug('block num: {}'.format(cache_config.num_gpu_blocks))


def cache_swapping(cache_engine: CacheEngine, swap_in_map: dict, swap_out_map: dict):
    """perform cache swapping."""
    issued_cache_op = False
    if len(swap_in_map) > 0:
        cache_engine.swap_in(swap_in_map)
        issued_cache_op = True
    if len(swap_out_map) > 0:
        cache_engine.swap_out(swap_out_map)
        issued_cache_op = True

    if issued_cache_op:
        cache_engine.events.wait()


@torch.inference_mode()
def model_forward(
    model: torch.nn.Module,
    inputs: ModelInputs,
    cache_engine: CacheEngine,
    world_size: int = 1,
    stream: torch.cuda.Stream = None,
):
    """perform model forward."""
    stream = stream or torch.cuda.current_stream()
    with torch.cuda.stream(stream):
        # forward
        ctx_mgr = model.ctx_mgr
        context = ctx_mgr.build_context(
            inputs=inputs,
            model_config=cache_engine.model_config,
            world_size=world_size,
            kv_caches=cache_engine.gpu_cache,
            kv_quant_policy=cache_engine.cache_config.quant_policy,
        )
        with ctx_mgr.context(context):
            model_metas = None
            model_metas = model.update_model_metas(
                past_key_values=cache_engine.gpu_cache,
                context=context,
            )
            input_dict = model.prepare_inputs_for_generation(
                past_key_values=cache_engine.gpu_cache,
                context=context,
            )
            output = model(**input_dict)
    return dict(hidden_states=output, model_metas=model_metas)


SwapMap = Dict[int, int]


class AutoModelAgent:
    """Base model agent."""

    def __init__(self, model_config: ModelConfig, cache_config: CacheConfig):
        self.model_config = model_config
        self.cache_config = cache_config

    async def async_forward(self, inputs: ModelInputs, swap_in_map: SwapMap, swap_out_map: SwapMap):
        """model forward.

        Args:
            inputs (Dict): The input data comes from _make_inputs.
            swap_in_map (SwapMap): Cache maps to swap in.
            swap_out_map (SwapMap): Cache maps to swap out.
        """
        raise NotImplementedError('Not implemented.')

    def get_logits(self, hidden_states: torch.Tensor):
        """get logits of model output."""
        raise NotImplementedError('Not implemented.')

    def get_input_processor(self):
        """get input processor."""
        raise NotImplementedError('Not implemented.')

    def close(self):
        """release model."""
        pass


class BaseModelAgent(AutoModelAgent):
    """Base model agent.

    load model on local gpu

    Args:
        model_path (str): The hugging face model path.
        model_config (ModelConfig): The config of the model.
        cache_config (CacheConfig): The config of the cache info.
        trust_remote_code (bool): Trust remote code
    """

    def __init__(self,
                 model_path: str,
                 model_config: ModelConfig,
                 cache_config: CacheConfig,
                 backend_config: BackendConfig,
                 adapters: Dict[str, str] = None,
                 trust_remote_code: bool = True):
        super().__init__(model_config=model_config, cache_config=cache_config)
        device = 'cuda'
        self.backend_config = backend_config
        self._adapters = adapters

        self.patched_model = self._build_model(model_path, adapters, device=device)

        _update_cache_config(model_config, cache_config)

        backend = get_backend()
        self.patched_model = backend.build_graph_runner(self.patched_model,
                                                        model_config=model_config,
                                                        cache_config=cache_config,
                                                        backend_config=backend_config,
                                                        device=device)

        self.cache_engine = CacheEngine(cache_config, model_config)

        self.stream = torch.cuda.Stream()

    def _build_model(self, model_path: str, adapters: Dict[str, str] = None, device: torch.device = 'cuda'):
        """build patched model."""
        custom_module_map = self.model_config.custom_module_map
        if custom_module_map is not None:
            update_custom_module_map(custom_module_map)
        logger.info('build model.')
        patched_model = build_patched_model(self.model_config, device=device)
        logger.info('loading weights.')
        load_model_weights(patched_model, model_path, device=device)
        logger.info('loading adapters.')
        if adapters is not None:
            add_adapters(patched_model, adapters, dtype=self.model_config.dtype, device=device)
        return patched_model

    def _forward_impl(self, inputs: ModelInputs, swap_in_map: SwapMap, swap_out_map: SwapMap):
        cache_swapping(self.cache_engine, swap_in_map=swap_in_map, swap_out_map=swap_out_map)
        output = model_forward(
            self.patched_model,
            inputs,
            self.cache_engine,
            world_size=1,
            stream=self.stream,
        )
        return output

    async def async_forward(self, inputs: ModelInputs, swap_in_map: SwapMap, swap_out_map: SwapMap):
        """model forward.

        Args:
            inputs (Dict): The input data comes from _make_inputs.
            swap_in_map (SwapMap): Cache maps to swap in.
            swap_out_map (SwapMap): Cache maps to swap out.
        """
        output = self._forward_impl(inputs, swap_in_map=swap_in_map, swap_out_map=swap_out_map)
        await asyncio.sleep(0)
        return output

    def get_logits(self, hidden_states: torch.Tensor):
        """get logits of model output."""
        return self.patched_model.get_logits(hidden_states)

    def get_input_processor(self):
        """get input processor.."""
        return self.patched_model.get_input_processor()

    def close(self):
        """release model."""
        self.patched_model = None
        self.cache_engine = None


@torch.inference_mode()
def _tp_build_model(
    rank: int,
    model_path: str,
    model_config: ModelConfig,
    cache_config: CacheConfig,
    backend_config: BackendConfig,
    adapters: Dict[str, str],
    world_size: int,
):
    """build tensor parallel model."""

    patched_model = None
    cache_engine = None

    def _broadcast_config(cache_config):
        """broadcast cache config, use minimum cache."""
        if rank == 0:
            gathered_configs = [None] * world_size
            dist.gather_object(cache_config, gathered_configs)
            num_gpu_blocks_list = [config.num_gpu_blocks for config in gathered_configs]
            num_cpu_blocks_list = [config.num_cpu_blocks for config in gathered_configs]
            min_num_gpu_blocks = min(num_gpu_blocks_list)
            min_num_cpu_blocks = min(num_cpu_blocks_list)
            cache_config.num_cpu_blocks = min_num_cpu_blocks
            cache_config.num_gpu_blocks = min_num_gpu_blocks
            config_list = [cache_config]
        else:
            gathered_configs = None
            dist.gather_object(cache_config, gathered_configs)
            config_list = [None]
        dist.broadcast_object_list(config_list)
        return config_list[0]

    try:
        device_map = torch.device('cuda')

        custom_module_map = model_config.custom_module_map
        if custom_module_map is not None:
            update_custom_module_map(custom_module_map)
        if rank == 0:
            logger.info('build model.')
        patched_model = build_patched_model(model_config, device=device_map)
        if rank == 0:
            logger.info('loading weights.')
        load_model_weights(patched_model, model_path, device=device_map)

        if adapters is not None:
            if rank == 0:
                logger.info('loading adapters.')
            add_adapters(patched_model, adapters, dtype=model_config.dtype, device=device_map)

        _update_cache_config(model_config, cache_config, gpu_id=rank, world_size=world_size)

        backend = get_backend()
        patched_model = backend.build_graph_runner(patched_model,
                                                   model_config=model_config,
                                                   cache_config=cache_config,
                                                   backend_config=backend_config,
                                                   device='cuda')

        cache_config = _broadcast_config(cache_config)
        cache_engine = CacheEngine(cache_config, model_config, rank=rank, world_size=world_size)

    except Exception as e:
        raise e

    return patched_model, cache_engine, cache_config


def _broadcast_inputs(rank: int, inputs: Any, group: dist.group, stream: torch.cuda.Stream):
    """get input tensor parallel."""
    # broadcast meta info
    if rank != 0:
        inputs = [None, None, None]
    else:
        device_inputs = inputs[0]
        meta_inputs = device_inputs.to_device('meta')
        inputs[0] = meta_inputs

    with torch.cuda.stream(stream):
        dist.broadcast_object_list(inputs, group=group)
        if rank == 0:
            device_inputs.broadcast()
        else:
            device_inputs = inputs[0].broadcast()

    inputs[0] = device_inputs

    return inputs


def _tp_model_loop(
    rank: int,
    model_path: str,
    model_config: ModelConfig,
    cache_config: CacheConfig,
    backend_config: BackendConfig,
    adapters: Dict[str, str],
    world_size: int,
    barrier: mp.Barrier,
    cpu_group: dist.group,
):
    """Start model loops for tensor parallel model inference.

    Args:
        rank (int): Distribution rank.
        model_path (int): Path of the hugging face model. Could be
            local or online.
        model_config (ModelConfig): The config of the model.
        cache_config (CacheConfig): The config of the cache.
        in_que (mp.Queue): Input queue. Used to receive model input.
        out_que (mp.Queue): Output queue. Used to send the model output.
        world_size (int): The distribution world size.
    """
    stream = torch.cuda.Stream()
    patched_model, cache_engine, _ = _tp_build_model(rank,
                                                     model_path,
                                                     model_config,
                                                     cache_config,
                                                     backend_config,
                                                     adapters=adapters,
                                                     world_size=world_size)

    while True:
        barrier.wait()
        inputs, swap_in_map, swap_out_map = _broadcast_inputs(rank, None, cpu_group, stream)

        cache_swapping(cache_engine, swap_in_map=swap_in_map, swap_out_map=swap_out_map)
        inputs = inputs.to_device('cuda')

        model_forward(
            patched_model,
            inputs,
            cache_engine,
            world_size=world_size,
            stream=stream,
        )


def _start_tp_process(proc_id: int,
                      world_size: int,
                      func: Callable,
                      log_level: int,
                      device_context: DeviceContext,
                      args: List = None,
                      kwargs: Dict = None):
    """Start the tensor parallel process.

    Args:
        rank (int): The distribution rank.
        world_size (int): The distribution world size.
        func (Callable): The function to be called in the process.
        args (List): The arguments of the func.
        kwargs (Dict): The keyword arguments of the func.
    """
    rank = proc_id + 1
    logger.setLevel(log_level)
    try:
        from lmdeploy.pytorch.check_env import check_env_deeplink
        check_env_deeplink(device_context.device_type)
        timeout = timedelta(days=35600)
        dist.init_process_group('nccl', rank=rank, world_size=world_size, timeout=timeout)
        cpu_group = dist.new_group(timeout=timeout, backend='gloo')
        kwargs['cpu_group'] = cpu_group
        dist_ctx = DistContext(rank=rank, world_size=world_size)
        torch.cuda.set_device(rank)
        with get_dist_manager().context(dist_ctx), get_device_manager().context(device_context), torch.inference_mode():
            args = args or tuple()
            kwargs = kwargs or dict()
            func(rank, *args, **kwargs)
    except threading.BrokenBarrierError:
        logger.warning(f'Rank[{rank}] exit.')
        # dist.destroy_process_group() may hang if using cudagraph
    except Exception as e:
        from traceback import print_exc
        logger.error(f'Rank[{rank}] failed.')
        print_exc()
        if dist.is_initialized():
            dist.destroy_process_group()
        raise e


def _check_context_alive(mp_context: mp.ProcessContext):
    """check context alive."""
    procs: List[mp.Process] = mp_context.processes
    failed_procs = list(idx for idx, p in enumerate(procs) if not p.is_alive())
    if len(failed_procs) == 0:
        return

    log_procs = []
    for idx, p in enumerate(procs):
        if p.is_alive():
            p.terminate()
        else:
            exitcode = p.exitcode
            if exitcode > 0:
                # terminated exitcode < 0
                log_procs.append((idx, exitcode))
            p.close()
    for idx, exitcode in log_procs:
        logger.error(f'TP process {idx} failed with exitcode {exitcode}.')
    # TODO: not safe exit.
    exit_code = 1 if len(log_procs) > 0 else 0
    os._exit(exit_code)


def _find_available_port() -> bool:
    """find available port."""
    import socket
    port = 29500
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
            port += 1


class TPModelAgent(AutoModelAgent):
    """Tensor Parallelism model agent.

    load model on multiple GPUs

    Args:
        model_path (str): The hugging face model path.
        model_config (ModelConfig): The config of the model.
        cache_config (CacheConfig): The config of the cache info.
        trust_remote_code (bool): Trust remote code
    """

    def __init__(self,
                 model_path: str,
                 model_config: ModelConfig,
                 cache_config: CacheConfig,
                 backend_config: BackendConfig,
                 world_size: int,
                 adapters: Dict[str, str] = None,
                 trust_remote_code: bool = True) -> None:
        import signal

        def __signal_term_handler(sig, frame):
            """sigterm handler."""
            if hasattr(self, 'mp_context'):
                procs = self.mp_context.processes
                for p in procs:
                    if p.is_alive():
                        p.kill()
            logger.error(f'Get signal[{sig}], kill all processes.')
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)

        super().__init__(model_config=model_config, cache_config=cache_config)

        signal.signal(signal.SIGTERM, __signal_term_handler)

        self.mp_ctx = mp.get_context('spawn')
        self.world_size = world_size
        self.backend_config = backend_config

        self._dist_ctx = None
        self.mp_bar = self.mp_ctx.Barrier(world_size)
        self._start_sub_process(model_path,
                                model_config=model_config,
                                cache_config=cache_config,
                                backend_config=backend_config,
                                adapters=adapters,
                                world_size=world_size,
                                barrier=self.mp_bar)

        model, cache_engine, cache_config = self._build_model(model_path=model_path,
                                                              model_config=model_config,
                                                              cache_config=cache_config,
                                                              backend_config=backend_config,
                                                              adapters=adapters,
                                                              world_size=world_size)
        self.patched_model = model
        self.cache_config = cache_config
        self.cache_engine = cache_engine
        self.stream = torch.cuda.Stream()

    def _mp_watchdog(self, mp_context: mp.ProcessContext, timeout: int = 1, stop_event: threading.Event = None):
        """watch dog of mp context.

        Args:
            mp_context: context of multiprocess.
            timeout: timeout
        """
        import time
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            _check_context_alive(mp_context)
            time.sleep(timeout)

    def _start_sub_process(self, model_path: str, model_config: ModelConfig, cache_config: CacheConfig,
                           backend_config: BackendConfig, adapters: Dict[str,
                                                                         str], world_size: int, barrier: mp.Barrier):
        """Start tensor parallel sub process."""
        port = _find_available_port()
        os.environ.setdefault('MASTER_ADDR', '127.0.0.1')
        os.environ.setdefault('MASTER_PORT', str(port))
        addr = os.environ['MASTER_ADDR']
        port = os.environ['MASTER_PORT']
        logger.info(f'MASTER_ADDR={addr}, MASTER_PORT={port}')

        device_context = get_device_manager().current_context()
        self.mp_context = mp.spawn(
            _start_tp_process,
            args=(
                world_size,
                _tp_model_loop,
                logger.level,
                device_context,
                (model_path, ),
                dict(
                    model_config=model_config,
                    cache_config=cache_config,
                    backend_config=backend_config,
                    adapters=adapters,
                    world_size=world_size,
                    barrier=barrier,
                ),
            ),
            nprocs=world_size - 1,
            join=False,
            daemon=True,
        )

        stop_event = threading.Event()
        t_watchdog = threading.Thread(target=self._mp_watchdog, args=[self.mp_context, 1.0, stop_event], daemon=True)
        t_watchdog.start()
        self.t_watchdog = t_watchdog
        self.t_watchdog.stop_event = stop_event

        rank = 0
        try:
            timeout = timedelta(days=35600)
            dist.init_process_group('nccl', rank=rank, world_size=world_size, timeout=timeout)
            cpu_group = dist.new_group(timeout=timeout, backend='gloo')
            dist_ctx = DistContext(rank=rank, world_size=world_size)
            self._dist_ctx = dist_ctx
            self._cpu_group = cpu_group
        except Exception as e:
            from traceback import print_exc
            logger.error(f'Rank[{rank}] failed.')
            print_exc()
            if dist.is_initialized():
                dist.destroy_process_group()
            raise e
        atexit.register(_exit_handler, self)

    @torch.inference_mode()
    def _build_model(
        self,
        model_path: str,
        model_config: ModelConfig,
        cache_config: CacheConfig,
        backend_config: BackendConfig,
        adapters: Dict[str, str],
        world_size: int,
    ):
        """build model."""
        with get_dist_manager().context(self._dist_ctx):
            rank = 0
            model, cache_engine, cache_config = _tp_build_model(
                rank,
                model_path=model_path,
                model_config=model_config,
                cache_config=cache_config,
                backend_config=backend_config,
                adapters=adapters,
                world_size=world_size,
            )

        return model, cache_engine, cache_config

    def _forward_impl(self, inputs: ModelInputs, swap_in_map: SwapMap, swap_out_map: SwapMap):
        """forward impl."""
        with get_dist_manager().context(self._dist_ctx):
            self.mp_bar.wait()
            rank = 0
            _broadcast_inputs(rank, [inputs, swap_in_map, swap_out_map], self._cpu_group, self.stream)

            cache_swapping(self.cache_engine, swap_in_map=swap_in_map, swap_out_map=swap_out_map)
            output = model_forward(
                self.patched_model,
                inputs,
                self.cache_engine,
                world_size=1,
                stream=self.stream,
            )
        return output

    async def async_forward(self, inputs: ModelInputs, swap_in_map: SwapMap, swap_out_map: SwapMap):
        """model forward.

        Args:
            inputs (Dict): The input data comes from _make_inputs.
            swap_in_map (SwapMap): Cache maps to swap in.
            swap_out_map (SwapMap): Cache maps to swap out.
        """
        output = self._forward_impl(inputs, swap_in_map=swap_in_map, swap_out_map=swap_out_map)
        await asyncio.sleep(0)
        return output

    def get_logits(self, hidden_states: torch.Tensor):
        """get logits of model output."""
        return self.patched_model.get_logits(hidden_states)

    def get_input_processor(self):
        """get input processor.."""
        return self.patched_model.get_input_processor()

    def close(self):
        """release model."""
        if hasattr(self, 'mp_context') and self.mp_context is not None:
            self.patched_model = None
            self.cache_engine = None
            self.t_watchdog.stop_event.set()
            self.t_watchdog.join()
            self.mp_bar.abort()
            procs = self.mp_context.processes
            for p in procs:
                if p.is_alive():
                    p.join()
                    p.close()
            self.mp_context = None
            if dist.is_initialized():
                if hasattr(self, '_cpu_group') and self._cpu_group is not None:
                    dist.destroy_process_group(self._cpu_group)
                    del self._cpu_group
                dist.destroy_process_group()


def _exit_handler(agent: TPModelAgent):
    if hasattr(agent, 'patched_model'):
        del agent.patched_model


def build_model_agent(model_path: str,
                      cache_config: CacheConfig,
                      backend_config: BackendConfig,
                      trust_remote_code: bool,
                      adapters: Dict[str, str] = None,
                      tp: int = 1,
                      dtype: str = 'auto',
                      custom_module_map: str = None):
    """create model agent.

    Args:
        model_path (str): the path of the input model
        cache_config (CacheConfig): config of kv cache
        backend_config (BackendConfig): config of backend devices
        trust_remote_code (bool): To use the remote modeling code or not
        adapters (Dict): lora adapters
        tp (int): the number of devices to be used in tensor parallelism
        dtype (str): the data type of model weights and activations
        custom_module_map (str): customized nn module map
    """
    model_config = ModelConfig.from_pretrained(model_path, trust_remote_code=trust_remote_code, dtype=dtype, tp=tp)
    model_config.custom_module_map = custom_module_map
    if tp == 1:
        model_agent = BaseModelAgent(model_path,
                                     model_config=model_config,
                                     cache_config=cache_config,
                                     backend_config=backend_config,
                                     adapters=adapters,
                                     trust_remote_code=trust_remote_code)
    else:
        model_agent = TPModelAgent(model_path,
                                   model_config=model_config,
                                   cache_config=cache_config,
                                   backend_config=backend_config,
                                   world_size=tp,
                                   adapters=adapters,
                                   trust_remote_code=trust_remote_code)
    return model_agent
