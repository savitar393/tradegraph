"""KB-driven Trade Compliance Document Preparation & Draft Generator (.docx / HTML / ZIP)."""

from tradegraph.documents.service import (
    DocumentFieldSpec,
    DocumentGeneratorService,
    DocumentTemplateSpec,
    PreparedDocument,
    PreparedDocumentPackage,
)

__all__ = [
    "DocumentFieldSpec",
    "DocumentGeneratorService",
    "DocumentTemplateSpec",
    "PreparedDocument",
    "PreparedDocumentPackage",
]
