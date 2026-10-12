import truststore

# Verify HTTPS against the OS certificate store instead of certifi's bundle, so outbound
# calls work behind TLS-inspecting proxies (e.g. Cloudflare Gateway, Zscaler) whose CA is
# installed on the machine but not shipped with Python.
truststore.inject_into_ssl()
