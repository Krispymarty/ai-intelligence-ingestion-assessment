import hashlib
import json

from pydantic import TypeAdapter

from .llm import Extractor, FieldClaim
from .store import Store

CLAIMS = TypeAdapter(list[FieldClaim])


async def extract_once(store: Store, extractor: Extractor, kind: str, text: str, *, title: str = ""):
    models = [(provider.name, provider.model) for provider in extractor.providers]
    payload = json.dumps(["fields-v1", models, kind, title, text], ensure_ascii=False)
    key = hashlib.sha256(payload.encode()).hexdigest()
    token, result = store.claim_extraction(key)
    if result is not None:
        return CLAIMS.validate_json(result), ["reused validated extraction"]
    if token is None:
        return [], ["another worker owns this extraction"]
    claims: list[FieldClaim] = []
    try:
        claims, events = await extractor.extract_fields(kind, text, title=title)
        return claims, events
    finally:
        store.complete_extraction(key, token, CLAIMS.dump_json(claims).decode() if claims else None)
