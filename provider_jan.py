"""Jan: Jan's local API server and its models."""

import llm_engine
from harnesses import Configure, Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.jan(processes)


def _installer():
    import install_ctl
    return install_ctl.Jan()


class Engine(llm_engine.LlamaRouter):
    """Jan's engine (jan-llama-worker) answers like llama.cpp's router."""
    config = ({'id': 'ctx_size',
      'label': 'Context size (total)',
      'role': 'context',
      'type': 'int',
      'default': 8192,
      'wire': {'yaml': 'ctx_size', 'ini': 'ctx-size'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'Context size (total)',
      'unit': 'tokens',
      'range': [128, 10000000]},
     {'id': 'ngl',
      'label': 'GPU layers',
      'role': 'gpu_layers',
      'type': 'int_or_special',
      'default': -2,
      'wire': {'yaml': 'n_gpu_layers', 'ini': 'n-gpu-layers'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'GPU layers',
      'unit': '',
      'range': [0, 999],
      'special': [-2, -1]},
     {'id': 'flash_attn',
      'label': 'Flash attention',
      'role': 'flash',
      'type': 'enum',
      'default': 'auto',
      'wire': {'yaml': 'flash_attn', 'ini': 'flash-attn'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'Flash attention',
      'unit': '',
      'values': ['on', 'off', 'auto']},
     {'id': 'cache_type_k',
      'label': 'Key cache',
      'role': 'cache_k',
      'type': 'enum',
      'default': 'f16',
      'wire': {'yaml': 'cache_type_k', 'ini': 'cache-type-k'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'Key cache',
      'unit': '',
      'values': ['f16', 'q8_0', 'q4_0']},
     {'id': 'cache_type_v',
      'label': 'Value cache',
      'role': 'cache_v',
      'type': 'enum',
      'default': 'f16',
      'wire': {'yaml': 'cache_type_v', 'ini': 'cache-type-v'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'Value cache',
      'unit': '',
      'values': ['f16', 'q8_0', 'q4_0']},
     {'id': 'parallel',
      'label': 'Parallel requests',
      'role': 'parallel',
      'type': 'int',
      'default': 1,
      'wire': {'yaml': 'parallel', 'ini': 'parallel'},
      'scope': 'per_model',
      'importance': 'main',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'Parallel requests',
      'unit': '',
      'range': [1, 8]},
     {'id': 'threads',
      'label': 'CPU threads',
      'role': 'threads',
      'type': 'int_or_special',
      'special': [-1],
      'default': -1,
      'wire': {'yaml': 'threads', 'ini': 'threads'},
      'scope': 'per_model',
      'importance': 'advanced',
      'writer': 'jan_pair',
      'restart': 'service',
      'verified': True,
      'help': 'CPU threads',
      'unit': '',
      'range': [1, 256]})

    ports = (6767, 1337)
    programs = ("jan-llama-worker", "jan-llama-worke")      # comm is cut to 15 characters

    def model_context(self, rt, name):
        import jan_ctl
        return jan_ctl.settings(name)[0]

    own_model_folder = False            # its models live in Jan's data folder; Jan moves it itself
    context_settable = True
    context_effect = "restart"

    def models_page_note(self, rt):
        from i18n import _
        return _("Jan adds models in its own app (Hub); here you set their context.")

    def context_now(self, rt, folder, name):
        import jan_ctl
        return jan_ctl.settings(name)

    def context_changes(self, rt, name, ctx):
        # Jan's preset (the engine) and the model's model.yml (the app), one backup.
        import jan_ctl
        return jan_ctl.context_changes(name, ctx)

    def data_folders(self, rt):
        import inventory
        return [str(inventory.jan_data_folder())]

    def add_model_step(self, rt):
        from i18n import _
        return _("add one in the Jan app (Hub), or right-click Jan on the map → Models….")


PROVIDER = Provider("jan", "Jan", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="jan-local", connect_label="Jan (local)",
                    install=(Way("installer"),),
                    configure=(Configure("Open Jan", ("jan-desktop",), window=True),),
                    homepage="https://jan.ai")
