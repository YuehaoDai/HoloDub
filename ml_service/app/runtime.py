from app.adapters.asr import ASRAdapter
from app.adapters.media import MediaSeparatorAdapter
from app.adapters.tts import TTSAdapter
from app.adapters.vad import VADAdapter
from app.config import Settings
from app.services.gpu_guard import GPUGuard
from app.services.lora_candidate import LoraCandidateService
from app.services.lora_training import LoraTrainingRunner
from app.services.model_manifest import ModelManifest
from app.services.model_registry import ModelRegistry


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        registry = ModelRegistry(max_models=settings.model_registry_max_models or None)
        self.settings = settings
        self.registry = registry
        self.gpu_guard = GPUGuard(settings.gpu_concurrency)
        self.model_manifest = ModelManifest(settings.model_manifest_path)
        self.separator = MediaSeparatorAdapter(settings)
        self.vad = VADAdapter(settings, registry)
        self.asr = ASRAdapter(settings, registry)
        self.tts = TTSAdapter(settings)
        self.lora_candidate = LoraCandidateService(settings, self.tts, self.asr)
        self.lora_training = LazyLoraTrainingRunner(settings)


class LazyLoraTrainingRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runner: LoraTrainingRunner | None = None
        self._init_error: FileNotFoundError | None = None

    def _get(self) -> LoraTrainingRunner:
        if self._runner is not None:
            return self._runner
        if self._init_error is not None:
            raise FileNotFoundError(str(self._init_error)) from self._init_error
        try:
            self._runner = LoraTrainingRunner(self.settings)
        except FileNotFoundError as exc:
            self._init_error = exc
            raise
        return self._runner

    def run_smoke(self, request):
        return self._get().run_smoke(request)

    def run_full(self, request):
        return self._get().run_full(request)

    def run_canary(self, request):
        return self._get().run_canary(request)

    def run_regression(self, request):
        return self._get().run_regression(request)
