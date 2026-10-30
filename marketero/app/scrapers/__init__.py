from .base import ContactRecord, extract_records, save_records  # noqa: F401
from .generic import GenericPageSource
from .tse import TSEPartidosSource

SOURCES = {s.key: s for s in (TSEPartidosSource(), GenericPageSource())}
