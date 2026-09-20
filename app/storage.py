from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from app.config import settings


class StorageBackend(ABC):
    """Abstração de storage para os uploads de fotos de recibo.

    Mantém o formato da *chave* idêntico ao `Foto.path` de hoje
    (`"uploads/{lista.id}_{i}.jpg"`) — só muda como os bytes são
    gravados/lidos/apagados e como a URL servível é derivada.
    """

    @abstractmethod
    def save(self, key: str, content: bytes, content_type: str = "image/jpeg") -> None:
        ...

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Lê os bytes gravados em `key`. Levanta `FileNotFoundError` se não existir."""
        ...

    @abstractmethod
    def url_for(self, key: str) -> str:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...


class LocalStorageBackend(StorageBackend):
    """Arquivos em app/static/, servidos pela rota autenticada /fotos/{key}."""

    def __init__(self, base_dir: str = "app/static") -> None:
        self.base_dir = Path(base_dir)

    def save(self, key: str, content: bytes, content_type: str = "image/jpeg") -> None:
        caminho = self.base_dir / key
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(content)

    def read(self, key: str) -> bytes:
        caminho = self.base_dir / key
        if not caminho.exists():
            raise FileNotFoundError(key)
        return caminho.read_bytes()

    def url_for(self, key: str) -> str:
        return f"/fotos/{key}"

    def delete(self, key: str) -> None:
        Path(self.base_dir, key).unlink(missing_ok=True)


class GCSStorageBackend(StorageBackend):
    """Usa google-cloud-storage com Application Default Credentials.

    Decisão de URL: o bucket é privado (fotos de recibo podem conter dados
    sensíveis, como CPF). Não há leitura pública nem signed URL — `url_for`
    aponta para a rota autenticada da própria aplicação (`/fotos/{key}`),
    que usa as credenciais do backend (Application Default Credentials /
    service account do Cloud Run) para buscar o objeto no GCS e devolver os
    bytes ao cliente já autenticado pelo `AuthMiddleware`.
    """

    def __init__(self, bucket_name: str) -> None:
        from google.cloud import storage

        self.bucket_name = bucket_name
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def save(self, key: str, content: bytes, content_type: str = "image/jpeg") -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(content, content_type=content_type)

    def read(self, key: str) -> bytes:
        from google.cloud.exceptions import NotFound

        blob = self._bucket.blob(key)
        try:
            return blob.download_as_bytes()
        except NotFound as exc:
            raise FileNotFoundError(key) from exc

    def url_for(self, key: str) -> str:
        return f"/fotos/{key}"

    def delete(self, key: str) -> None:
        from google.cloud.exceptions import NotFound

        blob = self._bucket.blob(key)
        try:
            blob.delete()
        except NotFound:
            pass


@lru_cache
def get_storage() -> StorageBackend:
    if settings.storage_backend == "gcs":
        return GCSStorageBackend(settings.gcs_bucket_name)

    return LocalStorageBackend()
