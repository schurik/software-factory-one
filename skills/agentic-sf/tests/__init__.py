# A package, on purpose. Another test directory in this repository is not one, and its modules do
# `from conftest import …`; if this directory were a plain one too, pytest
# would import both conftests as the top-level module `conftest` and whichever
# came second would shadow the first. As a package this one is
# `tests.conftest`, and the helpers are imported relatively.
