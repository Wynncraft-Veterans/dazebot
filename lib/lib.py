from enum import Enum


def unwrap[T](v: T | None) -> T:
    assert v is not None
    return v


class ProfCategory(Enum):
    PLEB = "pleb"
    VOID = "void"
    DERNIC = "dernic"
    TITANIUM = "titanium"
    CINNABAR = "cinnabar"
