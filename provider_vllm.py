"""vLLM: a vLLM server for one model."""

import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.vllm(processes)


def _installer():
    import install_ctl
    return install_ctl.Vllm()


class Engine(llm_engine.Engine):
    """One model per server, no unloading: freeing GPU memory stops it (the default)."""
    config = ({'id': 'max-model-len',
      'label': 'Context size',
      'role': 'context',
      'type': 'int',
      'default': 8192,
      'wire': 'max-model-len',
      'scope': 'global',
      'importance': 'main',
      'writer': 'command_advice',
      'restart': 'none',
      'verified': True,
      'help': 'Context size',
      'unit': 'tokens',
      'range': [128, 10000000]},
     {'id': 'gpu-memory-utilization',
      'label': 'GPU memory fraction',
      'role': 'utilization',
      'type': 'float',
      'default': 0.9,
      'wire': 'gpu-memory-utilization',
      'scope': 'global',
      'importance': 'main',
      'writer': 'command_advice',
      'restart': 'none',
      'verified': True,
      'help': 'GPU memory fraction',
      'unit': '',
      'range': [0.5, 0.95]},
     {'id': 'max-num-seqs',
      'label': 'Parallel requests',
      'role': 'parallel',
      'type': 'int',
      'default': 1,
      'wire': 'max-num-seqs',
      'scope': 'global',
      'importance': 'main',
      'writer': 'command_advice',
      'restart': 'none',
      'verified': True,
      'help': 'Parallel requests',
      'unit': '',
      'range': [1, 256]},
     {'id': 'kv-cache-dtype',
      'label': 'KV cache',
      'role': 'cache',
      'type': 'enum',
      'default': 'auto',
      'wire': 'kv-cache-dtype',
      'scope': 'global',
      'importance': 'main',
      'writer': 'command_advice',
      'restart': 'none',
      'verified': True,
      'help': 'KV cache',
      'unit': '',
      'values': ['auto', 'fp8']},
     {'id': 'tensor-parallel-size',
      'label': 'Graphics cards',
      'role': 'tensor',
      'type': 'int',
      'default': 1,
      'wire': 'tensor-parallel-size',
      'scope': 'global',
      'importance': 'advanced',
      'writer': 'command_advice',
      'restart': 'none',
      'verified': True,
      'help': 'Graphics cards',
      'unit': '',
      'range': [1, 256]})

    ports = (8000,)
    start_command = "vllm serve <model>"
    models_page = False                 # its one model is VLLM_MODEL in its env file

    def owns_process(self, comm, argv):
        if comm.startswith("VLLM") or (len(argv) > 1 and any(llm_engine.basename(a) == "vllm" for a in argv[:2])
                                       and "serve" in argv[1:3]):
            return llm_engine.BY_NAME
        return 0

    def add_model_step(self, rt):
        import install_ctl
        from i18n import _
        return _("vLLM serves one Hugging Face model: write its id into {0} (VLLM_MODEL=…), then start vLLM.") \
            .format(install_ctl.Vllm.env_file)


PROVIDER = Provider("vllm", "vLLM", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="vllm-local", connect_label="vLLM (local)",
                    install=(Way("installer"),),
                    data=("~/.cache/vllm",),
                    homepage="https://docs.vllm.ai")
