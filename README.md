# 青砾材料试验站采样交班

送检员根据送检单登记试验批和物理槽，装载整数光谱强度与设备位置。质检员登记有序工单，以站内采集接管的正原据签出整单。交班员按首签班次浏览实收，以成果号核对逐槽出处、强度合计和首执。设备关停后仍可领取及核账。

使用 Python 3.11 标准库。从项目根目录执行完整回归和班次演示。

```sh
python3 -B check_project.py
python3 -B -m station demo
```

`check_project.py` 由运行环境提供。在普通源码目录也可用 `python3 -B -m unittest discover -s tests -t . -v` 运行同一套测试。演示只在临时目录创建两个 SQLite 库。`samples/day.json` 是固定的送检数据，包含 A=7、B=11、C=13。X 按 A、B 签出 18，Y 按 B、C 签出 24。设备实取 B 一次，两单的 B 出处一致。

## 站内入口

`DeviceStore` 和 `Station` 支持上下文管理。用不同文件分别保存模拟设备和站库。班次通过打开 `Station` 时传入。停源工作时可省略设备参数。

```python
from device import open_device_store
from station import Station

with open_device_store('device.sqlite') as source:
    with Station('station.sqlite', source, shift='早班') as desk:
        desk.register_batch('批-01')
        desk.register_slots('批-01', ('A', 'B'))
        desk.load_sample('批-01', 'A', 7, '光谱台/01')
        desk.load_sample('批-01', 'B', 11, '光谱台/02')
        desk.order('X', '批-01', ('B', 'A'))
        collected = desk.resume('X')
        if not collected.pending:
            result = desk.issue('X')

with Station('station.sqlite', shift='晚班') as desk:
    result = desk.read('result:X')
    audit = desk.reconcile('早班', result.result_id)
```

- 批次与槽标识使用非空字符串。工单清单为同批已登记槽的有序序列，必须非空且无重复。同号同批同槽序复用，异参或非法输入抛出 `ValueError`。未知工单或成果抛出 `KeyError`。
- `load_sample` 校验站内登记。重复相同装载不重置设备库存，值或位置冲突时拒绝。`initialize(station, data)` 可以重复加载固定样例，保留已有批次、工单、实取进度和出处。
- `device.take` 按动作号及同批槽集合实取。重复同参返回原未确认回执，异参拒绝；已确认动作重投抛出 `ReceiptConfirmed`，包含原回执标识。`device.source_receipt` 可查询 `not_executed`、`unconfirmed`、`confirmed`，也可确认指定回执。设备暂停或返回丢失抛出可重试的 `DeviceUnavailable` 子类。确认后回执正文释放。
- `start_capture` 先持久登记动作再派发固定工单的槽，返回原回执或不确定状态。`resume` 可以从独立站内会话执行，接管相关动作的正原据并返回 `adopted`、`pending` 和仍需续办的 `retry_actions`。已接管原据可立即供所有相关工单使用，设备确认可独立重试。一个动作的回空不产生缺样结论。
- `issue` 只签发来源齐全的整单，返回 `IssuedResult` 或含待查槽的 `IssuePending`。每行保留槽号、整数读值及原出处。计量单位为设备光谱强度计数，精确整数相加，0 也须有正原据。拒绝提交后重新打开站库即可重投，重复及并行请求领取同一份成果及首执。
- `result_for` 按工单查已签成果，未签返回 `None`。`read` 按成果号领取原签件。`list_shift_results` 按工单号列出指定班次的实收。`export_sources` 按冻结槽序导出出处。`reconcile` 校对班次、清单、来源绑定、首执和重算合计，错班次或冻结记录不符抛出 `ValueError`。

## 持久边界

设备实取与设备回执同次提交。站库在另一个事务内持久接管整张回执的正原据，提交后才请求设备确认。回执确认可以幂等重投。签发再用独立站库事务冻结成果和首执。设备模拟器提供指定动作的暂停及提交后失答，站库提供指定事务的一次提交拒绝，以复现设备失答、接管失败及签发回滚。采集和签发资料均使用无损整数文本保存。已签成果依据自身冻结快照领取及核账。
