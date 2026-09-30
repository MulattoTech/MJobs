# Discovery spine

Own bounded mock-source HTTP, proven company/careers links, ATS tenant enrollment,
first-party enumeration and strong canonical route dedupe. Public interface:
discover(store, mock_origin, profile_id). Requirements: MJ-REQ-040, 041, 044, 052,
054, 063, 066, 067, 112, 114, 212. This initial adapter is explicitly MOCK;
production providers and broad parsing are not implemented or claimed here.
Depend on domain/persistence; provider quirks stay in adapters. Test:
python -m unittest tests.integration.test_spine. MockClient allows loopback by
default; trusted private Compose configuration alone permits the mock service
alias. Redirects and browser origins are constrained; HTML is data.
