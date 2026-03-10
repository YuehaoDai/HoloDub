from app.adapters.asr import ASRAdapter
from app.adapters.media import MediaSeparatorAdapter
from app.adapters.tts import TTSAdapter
from app.adapters.vad import VADAdapter
from app.config import Settings
from app.services.gpu_guard import GPUGuard
from app.services.model_manifest import ModelManifest
from app.services.model_registry import ModelRegistry


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        registry = ModelRegistry()
        self.settings = settings
        self.registry = registry
        self.gpu_guard = GPUGuard(settings.gpu_concurrency)
        self.model_manifest = ModelManifest(settings.model_manifest_path)
        self.separator = MediaSeparatorAdapter(settings)
        self.vad = VADAdapter(settings, registry)
        self.asr = ASRAdapter(settings, registry)
        self.tts = TTSAdapter(settings)
