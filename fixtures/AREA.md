# Fictional source/company/ATS environment

Own local HTTP fixture contract for MJ-REQ-209 through 213. Data describes Alex
Fixture and Example Systems Inc.; it is never user career data or production
discovery output. Interface: create_mock_app and fixture_server. Provider endpoints
are exposed only inside the private Compose network. Receipts have deterministic
application identity and durable mock storage. Tests exercise browser/HTTP/data
behavior; broader salary/remote/error/conditional variants remain PARTIAL.
