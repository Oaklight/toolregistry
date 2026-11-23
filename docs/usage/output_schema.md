# 输出 Schema

工具可以声明其返回值的结构。该 schema 保存在
`ToolMetadata.extra["output_schema"]` 中；LLM 发现与 MCP 传输会从那里读取，
并以 `outputSchema` 发布。核心库可以从函数的返回注解推导该 schema，因此带
类型注解的函数不必手写 JSON Schema。

推导默认关闭。发布 `outputSchema` 会改变 MCP 客户端看到的内容，因此部署层
可以单独开启，而无需修改工具定义。

## 开启推导

多数部署使用 registry 开关：

```python
from toolregistry import ToolRegistry

registry = ToolRegistry(derive_output_schemas=True)
```

通过 `registry.register()` 注册的每个函数都会以
`Tool.from_function(..., derive_output_schema=True)` 转换。类注册与可调用
覆盖走同一条路径。

只为一个工具开启时，直接构造 `Tool`：

```python
from toolregistry import Tool

tool = Tool.from_function(my_search, derive_output_schema=True)
```

两个开关都默认为 `False`，与旧版本的行为一致：除非工具自行声明，否则不会
凭空出现 `outputSchema`。

## 优先级与回退

顺序固定如下：

1. 显式的 `ToolMetadata.extra["output_schema"]` 优先。只要该键存在就足够，
   它永远不会被推导结果替换。
2. 否则，在开关开启时使用推导结果。
3. 否则，工具不发布输出 schema。

无法解析成完整 schema 的注解不会添加任何内容。核心库不会发布空的 `{}`
schema，因为那等于宣称一个从未描述过的契约。

```python
from toolregistry import Tool, ToolMetadata

declared = {"type": "object", "properties": {"path": {"type": "string"}}}

tool = Tool.from_function(
    find_file,
    metadata=ToolMetadata(extra={"output_schema": declared}),
    derive_output_schema=True,  # 被忽略：显式声明优先
)
```

## 支持的返回注解

- 基本类型：`str`、`int`、`float`、`bool`
- 容器：`list[T]`、`dict[str, T]`、`tuple[...]`，包括定长元组
- `Literal[...]` 与 `Enum` 子类
- `None` 与 `Optional[...]` / `T | None`，表示为可空
- `TypedDict`，包括 `total=False` 与 `NotRequired`
- dataclass，包括带默认值的字段
- pydantic `BaseModel` 子类

嵌套的 `TypedDict` 与 dataclass 定义会内联。pydantic 模型的 `$defs` 引用会在
返回前解析，因此不跟随 `$ref` 的客户端也能拿到完整 schema。递归结构不会生成
schema，也不会陷入循环。

## 语义约定

### 封闭对象与开放对象

`TypedDict` 与 dataclass 在类型系统中是封闭结构，因此推导出的 schema 设置
`additionalProperties: false`。普通 `dict[str, T]` 保持开放，`additionalProperties`
是值类型的 schema。pydantic 模型遵循自身配置：`extra="allow"` 的模型保持开放。

如果工具需要相反的契约，请显式声明 `extra["output_schema"]`。推导永远不会
悄悄翻转已发布的契约。

### 必填键

只有声明为必填的键会进入 `required`。

- `TypedDict` 中被 `NotRequired` 包裹的键，或 `total=False` 模型中的所有键，
  不会进入 `required`。
- 带默认值的 dataclass 字段不会进入 `required`。
- 可选键的类型仍会发布；变化的只是必填状态。

### 可空值

简单的可空结构使用类型列表，例如 `{"type": ["string", "null"]}`。包含更多
关键字的结构（枚举或嵌套对象）使用带 null 分支的 `anyOf`。

### 内联定义

嵌套的命名结构直接内联输出。顶层推导 schema 不含 `$defs` 或 `$ref`，因此
即使客户端不解析引用也能使用。

### 递归

引用自身的类型不会生成 schema。转换会检测正在访问的类型并停止；它不会循环，
也不会输出部分递归契约。

## 示例

```python
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from toolregistry import ToolRegistry


class Address(TypedDict):
    city: str
    postal_code: NotRequired[str]


@dataclass
class Customer:
    name: str
    address: Address
    note: str | None = None


registry = ToolRegistry(derive_output_schemas=True)


@registry.register
def load_customer(customer_id: int) -> Customer:
    """加载一位客户。"""
    ...
```

发布的 schema 描述一个封闭对象：`name` 与 `address` 必填，`note` 可选；
`address` 内联为对象，其唯一必填键是 `city`。
