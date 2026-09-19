from enum import StrEnum, auto


class AccountType(StrEnum):
    # Conta pertencente a um usuário final da plataforma
    USER: str = auto()
    # Conta de um jogo (ex: bingo, loteria, etc.)
    # Usada para receber valores pagos pelos usuários
    GAME: str = auto()
    # Conta de uma aplicação/tenant (caso tenha múltiplos sistemas integrados)
    APPLICATION: str = auto()
    # Conta da própria plataforma (ex: receitas, taxas, caixa geral)
    PLATFORM: str = auto()
    # Conta intermediária usada para segurar valores temporariamente
    # (ex: enquanto um pagamento externo não confirma)
    ESCROW: str = auto()
    # Conta específica para acumular taxas cobradas pela plataforma
    FEE: str = auto()


class EntryType(StrEnum):
    # Débito na conta: reduz o saldo da conta
    # Ex: usuário pagando um jogo
    DEBIT: str = auto()
    # Crédito na conta: aumenta o saldo da conta
    # Ex: usuário recebendo prêmio ou depósito confirmado
    CREDIT: str = auto()


class TransactionStatus(StrEnum):
    # Transação criada, mas ainda não efetivada
    # (ex: aguardando confirmação externa ou validações)
    PENDING: str = auto()
    # Transação efetivada (lançamentos já aplicados)
    POSTED: str = auto()
    # Transação foi revertida por outra (estorno)
    # Não será mais considerada como válida para saldo final
    # Ex: estorno de um pagamento que falhou após lançados os débitos/créditos
    REVERSED: str = auto()
    # Falha na execução (ex: internal error, inconsistência)
    FAILED: str = auto()
    # Cancelada antes de efetivar a transação (ex: usuário desistiu, timeout)
    # Ex: usuário cancelou antes da confirmação
    CANCELLED: str = auto()


class TransactionKind(StrEnum):
    # Transferencia entre contas controladas pelo Ledger.
    TRANSFER: str = auto()
    # Entrada de valor confirmada por um sistema de pagamento externo.
    EXTERNAL_CREDIT: str = auto()
    # Saida de valor confirmada por um sistema de pagamento externo.
    EXTERNAL_DEBIT: str = auto()


class HoldStatus(StrEnum):
    # Reserva ativa de saldo
    # Valor está indisponível para uso, mas ainda não foi debitado
    ACTIVE: str = auto()
    # Reserva consumida
    # O valor reservado foi efetivamente convertido em débito
    CONSUMED: str = auto()
    # Reserva liberada manualmente
    # Ex: pagamento externo falhou ou foi cancelado
    RELEASED: str = auto()
    # Reserva expirou automaticamente (timeout)
    # Ex: invoice LN expirou
    EXPIRED: str = auto()
    # Reserva cancelada explicitamente
    # Similar a RELEASED, mas indica ação direta do sistema/usuário
    CANCELLED: str = auto()
