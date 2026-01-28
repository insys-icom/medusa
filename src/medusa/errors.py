class MedusaError(Exception):
    def __str__(self) -> str:
        return ": ".join(self.args)


class SuiteError(MedusaError):
    def __init__(self, suite: str, *messages: str):
        super().__init__(f"Error in suite '{suite}'", *messages)


class VariableError(MedusaError):
    def __init__(self, var: str, *messages: str):
        super().__init__(f"Error in variable '{var}'", *messages)


class MetadataError(MedusaError):
    def __init__(self, meta: str, *messages: str):
        super().__init__(f"Error in metadata '{meta}'", *messages)
