from xml.etree import ElementTree

import httpx
import pytest

from services.discovery.sources.errors import SourceError
from services.discovery.sources.wwr import WeWorkRemotely


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_id_is_excluded_without_losing_other_listings(reverse):
    first = {"title": "Acme: Engineer", "link": "https://weworkremotely.com/remote-jobs/first"}
    second = {**first, "link": "https://weworkremotely.com/remote-jobs/second"}
    conflict = {**first, "link": "https://weworkremotely.com/remote-jobs/conflict"}
    pair = [conflict, {**conflict, "description": "Conflicting content"}]
    root = ElementTree.Element("rss")
    channel = ElementTree.SubElement(root, "channel")
    for row in [first, *(pair[::-1] if reverse else pair), second, first, conflict]:
        item = ElementTree.SubElement(channel, "item")
        for key, value in row.items():
            ElementTree.SubElement(item, key).text = value
    payload = ElementTree.tostring(root)
    adapter = WeWorkRemotely(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    )

    batch = adapter.search()
    assert [item.raw["link"] for item in batch.items] == [first["link"], second["link"]]
    assert not batch.complete_inventory
    assert adapter.health() == "PARTIAL"
    assert adapter.fetch("second")["link"] == second["link"]
    with pytest.raises(SourceError, match="SOURCE_DUPLICATE_JOB_ID"):
        adapter.fetch("conflict")
