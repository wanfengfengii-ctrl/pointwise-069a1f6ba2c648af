# 青砾材料试验站采样交班

送检员根据送检单登记试验批和物理槽，装载整数光谱强度与设备位置。质检员登记有序工单，以站内采集接管的正原据签出整单。交班员按首签班次浏览实收，以成果号核对逐槽出处、强度合计和首执。设备关停后仍可领取及核账。留样未实取的槽可以登记断班复验，在额度内为已签工单锁定共享留样并另行签出复验成果。

使用 Python 3.11 标准库。从项目根目录执行完整回归和班次演示。

```sh
python3 -B check_project.py
python3 -B -m station demo
```

`check_project.py` 由运行环境提供。在普通源码目录也可用 `python3 -B -m unittest discover -s tests -t . -v` 运行同一套测试。演示只在临时目录创建 SQLite 库。`samples/day.json` 是固定的送检数据，包含 A=7、B=11、C=13。X 按 A、B 签出 18，Y 按 B、C 签出 24。设备实取 B 一次，两单的 B 出处一致。演示另起一个复验场景：甲、乙两单各需一槽，X 仅支撑甲成本 2、Y 仅支撑乙成本 3、Z 同时支撑两单成本 4，额度 4 时只取共享 Z。

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

## 断班复验

复验面向已签原工单，从尚未实取的留样槽中取材。`plan_retests(requirements, candidates, budget)` 登记计划：

- `requirements` 为 `{工单号: 最少名额}` 映射，名额为正整数；每一单都必须已有签出成果，且同属一批。
- `candidates` 至多 24 项，每项形如 `(槽号, 可支撑工单集合, 耗材成本)`，成本为正整数。同一槽可同时支撑多单，但对每单至多提供一个名额；槽须已登记、尚未实取，且未被其它计划锁定。
- 在 `budget` 总额度内按 `(总成本, 槽数, 升序槽号)` 字典序选最小覆盖方案。覆盖不足或最小方案超额度时抛出 `RetestRefused`，其 `pending` 列出无法覆盖的工单，且不落任何计划、不锁任何槽。

计划一旦落库即持久锁定所选槽，`retest_plan(plan_id)` 返回 `RetestPlan`，含所选槽、各单冻结清单 `assignments`、关联原成果与首执 `original_results`、`original_receipts`。同一参数重投返回原计划；同号异参抛出 `ValueError`。

- 普通采集不得先取已锁留样：`resume` 把锁定槽留在 `pending`，`start_capture` 对锁定槽抛出 `SampleLocked`。锁持续到销账；复验实取失答时保持留样锁。
- `resume_retest(plan_id, action_id=None)` 仍经原设备实取：先登记动作，再经设备回执接管与确认。返回 `adopted`、`pending` 和可重试的 `retry_actions`，语义与普通 `resume` 一致。一个动作的失答不产生缺样结论。
- `settle_retests(plan_id)` 在全部名额均有实际原据后，按各单冻结清单签出复验成果；共享槽只取一次。尚未齐备时返回带 `pending` 的 `RetestSettlement` 并保持锁。成果保留关联的原成果号和原首执，`retest_for`、`read_retest`、`list_shift_retests`、`reconcile_retest` 分别按工单查、按成果号领、按班次列、按班次核对。

## 持久边界

设备实取与设备回执同次提交。站库在另一个事务内持久接管整张回执的正原据，提交后才请求设备确认。回执确认可以幂等重投。签发再用独立站库事务冻结成果和首执。设备模拟器提供指定动作的暂停及提交后失答，站库提供指定事务的一次提交拒绝，以复现设备失答、接管失败及签发回滚。采集和签发资料均使用无损整数文本保存。已签成果依据自身冻结快照领取及核账。

复验计划、锁、实取动作、原据与复验成果同样各自成事务：计划事务写入选中槽、各单冻结清单和留样锁；实取沿用设备实取与回执接管；销账在单个站库事务内签出全部复验成果并释放锁。`reject_next_commit('plan')` 与 `reject_next_commit('settle')` 可在提交前拒绝，复现计划回滚与销账回滚。拒办、失答重开、回执补认和原成果复领沿用既有语义。复验成果与原成果互不覆盖，`result_for` 仍返回原签件。
