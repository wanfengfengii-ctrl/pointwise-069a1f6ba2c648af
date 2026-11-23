from dataclasses import dataclass


@dataclass(frozen=True)
class ResultLine:
    slot: str
    value: int
    source_batch: str
    source_slot: str
    position: str
    action_id: str
    device_receipt_id: str


def measure_total(lines):
    total = 0
    for line in lines:
        if type(line.value) is not int:
            raise ValueError(f'invalid frozen strength for slot {line.slot!r}')
        total += line.value
    return total
