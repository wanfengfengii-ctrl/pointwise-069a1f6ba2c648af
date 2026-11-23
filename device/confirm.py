from orders.batches import require_text


def confirm_receipt(store, action_id, receipt_id):
    action_id = require_text(action_id, 'action id')
    receipt_id = require_text(receipt_id, 'device receipt id')
    store.require_available('confirm', action_id)
    with store.write_transaction():
        row = store.db.execute(
            'SELECT receipt_id, confirmed FROM device_actions WHERE action_id = ?', (action_id,)
        ).fetchone()
        if row is None or row['receipt_id'] != receipt_id:
            raise ValueError(f'receipt does not belong to device action {action_id!r}')
        if row['confirmed']:
            return receipt_id
        store.db.execute(
            'UPDATE device_actions SET confirmed = 1, body = NULL WHERE action_id = ?', (action_id,)
        )
    store.deliver_reply('confirm', action_id)
    return receipt_id
