import json

import pytest

from test_publish import cli, load_cli, site, stub


HTML = '''<!doctype html>
<base href="/">
<link href="/assets/app.css" rel="stylesheet">
<img src="/assets/a.png" srcset="/assets/a.png 1x, /assets/b.png 2x, //cdn.example/c.png 3x">
<video poster="/assets/poster.png"></video>
<div style="background: url('/assets/inline.png')"></div>
<style>
@import "/assets/import.css";
p { background:url(/assets/style.png) }
</style>
<script src="//cdn.example/script.js"></script>
<img src="./relative.png"><a href="#anchor">link</a>
'''


@pytest.mark.parametrize("dry_run", [False, True])
def test_root_reference_warnings_are_shared_and_stderr_only(cli, site, dry_run, capsys):
    (site / "index.html").write_text(HTML)
    (site / "assets/app.css").write_text('@import url("/assets/more.css");\n.x{background:url(/assets/bg.png)}\n.y{background:url(//cdn.example/bg.png)}')
    args = [str(site), "--ttl", "1d", "--json"] + (["--dry-run"] if dry_run else [])
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    for address in ("/assets/app.css", "/assets/a.png", "/assets/b.png", "/assets/poster.png",
                    "/assets/inline.png", "/assets/import.css", "/assets/style.png",
                    "/assets/more.css", "/assets/bg.png"):
        assert address in captured.err
    assert 'index.html:2: 站点根 URL "/"' in captured.err
    assert "index.html:3:" in captured.err
    assert "app.css:2:" in captured.err
    assert "cdn.example" not in captured.err
    assert "./relative.png" not in captured.err
    assert "#anchor" not in captured.err
    if not dry_run:
        assert json.loads(captured.out)["file_count"] == 1
    else:
        assert json.loads(captured.out)["dry_run"] is True
        assert cli.stub_state.posts == []


def test_css_root_url_split_across_chunks_and_line_numbers(capsys):
    cli = load_cli()
    scanner = cli.CSSWarnings("app.css")
    scanner.feed("/* one */\n.x { background: url('/asse")
    scanner.feed("ts/a.png') }\n.y{background:url(/assets/b.png)}")
    scanner.feed("\n.z{background:url(//cdn.test/no.png)}")
    err = capsys.readouterr().err
    assert err.count("/assets/a.png") == 1
    assert err.count("/assets/b.png") == 1
    assert 'app.css:2:' in err
    assert 'app.css:3:' in err
    assert 'cdn.test' not in err
    assert len(scanner.tail) <= cli.WARNING_OVERLAP


def test_streaming_html_handles_bounded_chunks_and_warns_on_oversized_tag(cli, site, capsys):
    (site / "index.html").write_text('<!doctype html>\n' + 'x' * (cli.WARNING_CHUNK * 3) +
                                     '\n<img src="/assets/late.png">')
    assert cli.main([str(site), "--ttl", "1d", "--dry-run"]) == 0
    assert "index.html:3:" in capsys.readouterr().err
    (site / "index.html").write_text('<img title="' + 'x' * (cli.WARNING_CHUNK * 3))
    assert cli.main([str(site), "--ttl", "1d", "--dry-run"]) == 0
    assert "超长 HTML" in capsys.readouterr().err


def test_multiline_html_attribute_reports_actual_line(capsys):
    cli = load_cli()
    scanner = cli.HTMLWarnings("index.html")
    scanner.feed('<img\n src="/assets/a.png"\n srcset="/assets/b.png 1x,\n /assets/c.png 2x">')
    err = capsys.readouterr().err
    assert 'index.html:2: 站点根 URL "/assets/a.png"' in err
    assert 'index.html:3: 站点根 URL "/assets/b.png"' in err
    assert 'index.html:4: 站点根 URL "/assets/c.png"' in err
