import uuid
from typing import Optional
from fastapi import Header

def get_correlation_id(x_correlation_id: Optional[str] = Header(None)):
    return x_correlation_id or str(uuid.uuid4())
