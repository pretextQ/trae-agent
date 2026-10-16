"""Safe model failures; diagnostics are allowlisted, never raw response bodies."""


class ModelError(Exception):
    def __init__(self, code, message, diagnostics=None):
        super().__init__(message)
        self.code, self.message, self.diagnostics = code, message, diagnostics or {}


class BudgetExhausted(ModelError):
    def __init__(self, spent, budget):
        super().__init__(
            "budget_exhausted", f"Model budget reached: {spent:.4f} of {budget:.4f} RMB"
        )
