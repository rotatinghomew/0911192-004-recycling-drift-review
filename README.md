# 回收分拣留样的质量漂移追查

分拣运转、批次流向及同留样多规程测定记录。
这里的记录全部为合成资料。日期按当地日历解释，未附时区的时间使用东八区。

## 交接资料

CSV 使用 UTF-8 和逗号分隔。值留空表示这条资料未给出该字段，不等于零。
`data/schema.json` 登记表头与跨表编号关系；`data/policy.json` 保存本期业务配置。

- `data/feed_lots.csv`：进料称重含已知材料档次；来源档次不是质量测量。 字段：`lot`, `supplier`, `grade`, `in_kg`, `intake`。
- `data/mix.csv`：每行是加工批次中来自一个进料批的重量范围；同批与同源分别保持守恒。 字段：`batch`, `line`, `processed`, `input`, `kg_min`, `kg_max`。
- `data/retains.csv`：mixed 表示评级来自同样本混合；还未销毁时才有复测机会。 字段：`sample`, `batch`, `grade`, `kind`, `sampled`, `retain_until`, `disposal`。
- `data/assays.csv`：method 为规程号；相同 sample 的独立读数可配对；只有授权结果可用于处置。 字段：`read`, `sample`, `method`, `fraction`, `recorded`, `authorized`。
- `data/shipments.csv`：同一出货包含多加工批，separable 说明是否还能现场分别拣回。 字段：`shipment`, `batch`, `outlet`, `separable`。
- `data/holds.csv`：当班实际暂停或放行的记录。 字段：`decision`, `shipment`, `effective`, `state`。

## 资料校验

使用 Python 3.11.2 标准库，不需要安装扩展包。资料入口只核查交接格式，不计算业务结果。

```sh
python3 inspect_inputs.py
python3 -m unittest discover -s tests -v
```
