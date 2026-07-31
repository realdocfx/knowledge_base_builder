#!/usr/bin/env python3
"""Generate a real multi-entry ZIM for the FUSE validation job.

Kept out of the workflow YAML deliberately: a Python heredoc nested inside a YAML
`run:` block repeatedly mangled its closing delimiter and its backslash escapes.
A real file has none of that fragility and can be run and linted on its own.
"""
import sys

from libzim.writer import Creator, Hint, Item, StringProvider


class Page(Item):
    def __init__(self, path, title, content):
        super().__init__()
        self._p, self._t, self._c = path, title, content

    def get_path(self):
        return self._p

    def get_title(self):
        return self._t

    def get_mimetype(self):
        return "text/html"

    def get_contentprovider(self):
        return StringProvider(self._c)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "test.zim"
    with Creator(out).config_indexing(True, "eng") as c:
        c.set_mainpath("index")
        c.add_item(Page("index", "Home", "<html><body>home</body></html>"))
        for i in range(6000):
            body = "<html><body>" + (f"page {i} " * 300) + "</body></html>"
            c.add_item(Page(f"page{i}", f"Page {i}", body))
    print(f"generated {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
