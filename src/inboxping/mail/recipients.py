from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from email.utils import getaddresses


@dataclass(frozen=True)
class RecipientSummary:
    count: int
    preview: str
    prompt_context: str
    addresses: tuple[str, ...]


def summarize_recipients(value: str) -> RecipientSummary:
    addresses = [address.strip() for _, address in getaddresses([value]) if address.strip()]
    if not addresses:
        return RecipientSummary(count=0, preview="", prompt_context="未记录", addresses=())

    count = len(addresses)
    samples = [address[:100] for address in addresses[:3]]
    domains = Counter(address.rsplit("@", 1)[-1].lower() for address in addresses if "@" in address)
    domain_text = "、".join(
        f"{domain[:80]}（{amount} 人）" for domain, amount in domains.most_common(5)
    )
    context = f"共 {count} 人；地址示例：{', '.join(samples)}"
    if domains:
        context += f"；涉及 {len(domains)} 个域名，主要为：{domain_text}"
    if count > len(samples):
        context += "；其余地址未提供"
    return RecipientSummary(
        count=count,
        preview=", ".join(samples),
        prompt_context=context,
        addresses=tuple(addresses),
    )
