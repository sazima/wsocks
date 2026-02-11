import ipaddress
import logging
from typing import List, Set, Tuple, Union

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

logger = logging.getLogger('socks5-ws-proxy')


class Action:
    PROXY = 'proxy'
    DIRECT = 'direct'
    BLOCK = 'block'


class RuleType:
    DOMAIN_SUFFIX = 'domain_suffix'
    DOMAIN = 'domain'
    DOMAIN_KEYWORD = 'domain_keyword'
    IP_CIDR = 'ip_cidr'
    RULESET = 'ruleset'  # 仅用于加载阶段，运行时不存储


class RuleConfig(TypedDict, total=False):
    type: str
    action: str
    value: Union[str, List[str]]
    path: str


class RoutingConfig(TypedDict, total=False):
    default: str
    rules: List[RuleConfig]


# 运行时存储的规则元组类型
_Rule = Tuple[str, object, str]  # (rule_type, value, action)


class Router:
    """分流路由器：根据目标地址决定走直连还是代理

    规则类型（按声明顺序匹配，首次命中即返回）：
      domain_suffix  - 后缀匹配，.cn 匹配 example.cn 及 sub.example.cn
      domain         - 完整域名精确匹配
      domain_keyword - 关键词包含匹配
      ip_cidr        - IP 段匹配，如 192.168.0.0/16
      ruleset        - 加载阶段专用，从文件展开为以上类型，运行时不存储

    动作：Action.PROXY / Action.DIRECT / Action.BLOCK
    未命中任何规则则返回 default 动作。
    """

    def __init__(self, config: RoutingConfig):
        self.default_action: str = config.get('default', Action.PROXY)
        self._rules: List[_Rule] = []
        self._load_rules(config.get('rules', []))
        logger.info(
            f"Router initialized: default={self.default_action}, "
            f"{len(self._rules)} rule group(s)"
        )

    def _load_rules(self, rules_config: List[RuleConfig]) -> None:
        for rule in rules_config:
            rule_type = rule.get('type', '')
            action = rule.get('action', Action.PROXY)
            value = rule.get('value', [])

            if isinstance(value, str):
                value = [value]

            if rule_type == RuleType.DOMAIN_SUFFIX:
                suffix_set: Set[str] = {v.lower().lstrip('.') for v in value}
                self._rules.append((RuleType.DOMAIN_SUFFIX, suffix_set, action))

            elif rule_type == RuleType.DOMAIN:
                domain_set: Set[str] = {v.lower() for v in value}
                self._rules.append((RuleType.DOMAIN, domain_set, action))

            elif rule_type == RuleType.DOMAIN_KEYWORD:
                keywords: List[str] = [v.lower() for v in value]
                self._rules.append((RuleType.DOMAIN_KEYWORD, keywords, action))

            elif rule_type == RuleType.IP_CIDR:
                networks = []
                for v in value:
                    try:
                        networks.append(ipaddress.ip_network(v, strict=False))
                    except ValueError as e:
                        logger.warning(f"Invalid IP CIDR '{v}': {e}")
                if networks:
                    self._rules.append((RuleType.IP_CIDR, networks, action))

            elif rule_type == RuleType.RULESET:
                path = rule.get('path', '')
                if path:
                    self._load_ruleset_file(path, action)

    def _load_ruleset_file(self, path: str, default_action: str) -> None:
        """从文件加载规则，展开为标准类型追加到 _rules

        支持两种格式：
          1. 纯域名列表（每行一个域名，视为 domain_suffix）
          2. Surge/Clash 格式：DOMAIN-SUFFIX,x.com / DOMAIN,x.com / DOMAIN-KEYWORD,kw
        """
        suffixes: Set[str] = set()
        domains: Set[str] = set()
        keywords: List[str] = []

        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if ',' in line:
                        parts = line.split(',', 1)
                        rtype = parts[0].strip().upper()
                        rval = parts[1].strip().lower()
                        if rtype in ('DOMAIN-SUFFIX', 'DOMAIN_SUFFIX'):
                            suffixes.add(rval.lstrip('.'))
                        elif rtype in ('DOMAIN', 'DOMAIN-FULL'):
                            domains.add(rval)
                        elif rtype in ('DOMAIN-KEYWORD', 'DOMAIN_KEYWORD'):
                            keywords.append(rval)
                    else:
                        # 纯域名，视为 suffix（匹配本身及所有子域名）
                        suffixes.add(line.lower().lstrip('.'))

            if suffixes:
                self._rules.append((RuleType.DOMAIN_SUFFIX, suffixes, default_action))
            if domains:
                self._rules.append((RuleType.DOMAIN, domains, default_action))
            if keywords:
                self._rules.append((RuleType.DOMAIN_KEYWORD, keywords, default_action))

            logger.info(
                f"Loaded ruleset '{path}': {len(suffixes)} suffixes, "
                f"{len(domains)} domains, {len(keywords)} keywords, action={default_action}"
            )
        except FileNotFoundError:
            logger.error(f"Ruleset file not found: {path}")
        except Exception as e:
            logger.error(f"Failed to load ruleset '{path}': {e}")

    def decide(self, host: str) -> str:
        """根据目标地址返回路由动作：Action.PROXY / Action.DIRECT / Action.BLOCK"""
        # 优先尝试 IP 地址匹配
        try:
            ip = ipaddress.ip_address(host)
            for rule_type, value, action in self._rules:
                if rule_type == RuleType.IP_CIDR:
                    for network in value:
                        if ip in network:
                            return action
        except ValueError:
            pass

        # 域名规则匹配（按声明顺序，首次命中即返回）
        host_lower = host.lower()
        labels = host_lower.split('.')
        for rule_type, value, action in self._rules:
            if rule_type == RuleType.DOMAIN:
                if host_lower in value:
                    return action
            elif rule_type == RuleType.DOMAIN_SUFFIX:
                # 逐级向上查找：sub.example.com → example.com → com，O(L) hash 查找
                for i in range(len(labels)):
                    if '.'.join(labels[i:]) in value:
                        return action
            elif rule_type == RuleType.DOMAIN_KEYWORD:
                for keyword in value:
                    if keyword in host_lower:
                        return action

        return self.default_action
