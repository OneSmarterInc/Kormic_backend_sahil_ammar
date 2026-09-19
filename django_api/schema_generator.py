from drf_spectacular.generators import SchemaGenerator


class ClientContractSchemaGenerator(SchemaGenerator):
    """Deployment checks cover the same scoped schema that /api/v1/schema publishes."""
    def __init__(self, *args, **kwargs):
        kwargs['urlconf'] = kwargs.get('urlconf') or 'django_api.contract_urls'
        super().__init__(*args, **kwargs)
