from capture.actions import mark_receipt_released
from capture.evidence import get_committed_receipt
from device.recover import source_receipt


def release_receipt(station, action_id, *, confirmed_receipt_id=None):
    committed = get_committed_receipt(station, action_id)
    if committed is None:
        raise ValueError(f'action {action_id!r} lacks a committed receipt')
    if confirmed_receipt_id is None:
        state = source_receipt(station.source, action_id, confirm=committed)
        if state.status != 'confirmed' or state.action_id != action_id:
            raise ValueError(f'device did not confirm action {action_id!r}')
        confirmed_receipt_id = state.receipt_id
    if confirmed_receipt_id != committed:
        raise ValueError(f'confirmed receipt differs for action {action_id!r}')
    with station.write('release'):
        mark_receipt_released(station, action_id, committed)
