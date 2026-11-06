from urllib.parse import urlparse

def _base_url(apim_openai_endpoint) -> str:
    endpoint = apim_openai_endpoint

    if not endpoint:
        raise ValueError("APIM_OPENAI_ENDPOINT is not set")

    endpoint = endpoint.rstrip("/")

    parsed = urlparse(endpoint)

    # If a full URL is provided, return only scheme + host
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"

    return endpoint


print(_base_url("https://rokaigateway-nonprod.azure.rockwellautomation.com"))