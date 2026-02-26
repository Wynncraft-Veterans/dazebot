from typing import TypeVar, Optional

T = TypeVar("T")

def unwrap(v: Optional[T]) -> T:
    assert v is not None
    return v