"""YouTube/link discovery, dates, signed-URL cache identity and CLI (no network)."""
import io
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from sofit import cli, footage as f, footage_selection as fs, footage_youtube as yt
from sofit.footage_direct import DirectProvider

URL = 'https://www.youtube.com/watch?v=abcdefghijk'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(f, 'open_url', lambda *a, **k: pytest.fail('unexpected network'))
    monkeypatch.setattr(yt, '_extract', lambda *a, **k: pytest.fail('unexpected extractor'))


def info(**updates):
    result = dict(webpage_url=URL, title='Unitree robot running', duration=30,
                  width=1280, height=720, url='https://r1.googlevideo.com/videoplayback?expire=1',
                  protocol='https', format_id='136', channel='Unitree', channel_url='https://www.youtube.com/@unitree',
                  upload_date='20260918', license='Standard YouTube License',
                  description='Real robot demonstration', tags=['Unitree', 'robot'],
                  subtitles={'en': [{'ext': 'vtt', 'url': 'https://www.youtube.com/api/timedtext?v=abcdefghijk'}]})
    result.update(updates)
    return result


@pytest.mark.parametrize('url', [URL + '&list=anything', 'https://youtu.be/abcdefghijk?t=3',
                                'https://www.youtube.com/shorts/abcdefghijk'])
def test_individual_video_identity(url):
    assert yt.youtube_url(url) == URL


@pytest.mark.parametrize('url', ['https://youtube.com.evil.test/watch?v=abcdefghijk',
                                'https://www.youtube.com/playlist?list=anything',
                                'https://www.youtube.com:8443/watch?v=abcdefghijk',
                                'https://youtu.be/bad'])
def test_rejects_non_video_and_lookalike_links(url):
    assert yt.youtube_url(url) is None


def test_normalization_preserves_provenance_without_inventing_rights():
    c = yt.normalize_candidate(info())
    assert c.provider == 'youtube' and c.published_at == '2026-09-18'
    assert c.creator == 'Unitree' and c.channel_url.endswith('@unitree')
    assert c.original_media_url == c.media_url and c.subtitle_urls
    assert c.license == 'Standard YouTube License' and c.attribution_required is None
    assert f.license_allowed(c) and not f.license_allowed(c, True)
    changed = yt.normalize_candidate(info(url='https://r2.googlevideo.com/videoplayback?expire=2'))
    assert changed.cache_url == c.cache_url
    assert yt.normalize_candidate(info(license=None)).license == 'unknown'


@pytest.mark.parametrize('updates', [{'is_live': True}, {'has_drm': True}, {'age_limit': 18},
                                     {'availability': 'private'}, {'protocol': 'm3u8_native'},
                                     {'url': 'https://googlevideo.com.evil.test/video'}, {'upload_date': 'invalid'}])
def test_unavailable_or_unsupported_results_are_skipped(updates):
    assert yt.normalize_candidate(info(**updates)) is None


def test_search_uses_upload_window_then_verifies_exact_dates(monkeypatch):
    calls = []
    old = 'https://www.youtube.com/watch?v=oldoldold12'
    def extract(target, flat=False):
        calls.append((target, flat))
        if flat:
            return {'entries': [{'url': old, 'title': 'Unitree robot running', 'duration': 20},
                                {'url': URL, 'title': 'Unitree robot running', 'duration': 30}]}
        return info(webpage_url=target, upload_date='20260101' if target == old else '20260918')
    monkeypatch.setattr(yt, '_extract', extract)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    yt.YouTubeProvider(published_after=cutoff).search('Unitree robot',8)
    assert calls[0][0].startswith('https://www.youtube.com/results?')
    assert parse_qs(urlsplit(calls[0][0]).query)['sp'] == ['EgQIAxAB']
    result = yt.YouTubeProvider(published_after='2026-09-01').search('Unitree robot',8)
    assert len(result) == 1 and result[0].source_url == URL
    calls.clear()
    yt.YouTubeProvider().search('Unitree robot',2)
    assert calls[0] == ('ytsearch2:Unitree robot', True)


def test_search_is_bounded_and_one_failed_video_does_not_hide_others(monkeypatch):
    calls = []
    def extract(target, flat=False):
        if flat:
            return {'entries': [{'url': 'https://www.youtube.com/watch?v=abcdefghij' + str(i), 'title': 'robot', 'duration': 30} for i in range(8)]}
        calls.append(target)
        if len(calls) == 1:
            raise f.FootageError('unavailable')
        return info()
    monkeypatch.setattr(yt, '_extract', extract)
    assert len(yt.YouTubeProvider().search('robot',20)) == 3
    assert len(calls) == 4


def test_date_filter_and_recency_never_rescue_unrelated_results():
    c = yt.normalize_candidate(info())
    old = replace(c, published_at='2020-01-01', cache_url=URL+'&old=1')
    recent = replace(c, published_at=datetime.now(timezone.utc).date().isoformat())
    intent = f.VisualIntent('running robot','Unitree robot running',4,prefer_recent=True)
    assert f.rank_candidates([old,recent],intent) == [recent,old]
    assert f.rank_candidates([old,c,replace(c,published_at='')],replace(intent,published_after='2026-09-01')) == [c]
    assert f.rank_candidates([replace(recent,title='sunset',description='',tags=())],intent) == []


def test_explicit_links_need_visual_review_but_not_keyword_filenames():
    c = f.Candidate('direct','https://example.org/123.mp4','https://example.org/123.mp4','123.mp4',30,1280,720)
    intent = f.VisualIntent('robot running','robot running',4,source_urls=(c.source_url,))
    assert f.rank_candidates([c],intent) == [c]
    assert f.rank_candidates([c],intent,True) == []
    assert f.rank_candidates([c],replace(intent,published_after='2026-01-01')) == []


def test_expired_media_url_reuses_valid_cached_video(monkeypatch,tmp_path):
    c = yt.normalize_candidate(info())
    class Response(io.BytesIO):
        headers = {'Content-Length': '5'}
    calls = []
    def download(*args):
        calls.append(args)
        return Response(b'video')
    monkeypatch.setattr(f,'open_url',download)
    monkeypatch.setattr(f,'probe_video',lambda *a: f.VideoInfo(30,1280,720))
    cache = f.FootageCache(tmp_path)
    first = cache.retrieve(c)
    second = cache.retrieve(replace(c,media_url='https://other.googlevideo.com/expired'))
    assert first == second and len(calls) == 1


def test_direct_probe_and_unknown_provenance(monkeypatch,tmp_path):
    class Cache:
        def retrieve(self,c):
            assert c.media_url == 'https://example.org/123.mp4'
            return tmp_path/'video',{'video':{'duration':12,'width':640,'height':360},'size':30}
    c = DirectProvider().resolve('https://example.org/123.mp4#t=5',Cache())
    assert c.duration == 12 and c.width == 640 and c.license == 'unknown'
    assert c.source_url.endswith('123.mp4')


def test_explicit_safe_unknown_direct_link_does_not_download(monkeypatch):
    monkeypatch.setattr(DirectProvider,'resolve',lambda *a: pytest.fail('safe-only unknown direct source'))
    intent = f.VisualIntent('robot','robot',4,source_urls=('https://example.org/video.mp4',))
    assert fs.find_footage(intent,providers=[],safe_only=True) is None


def test_cli_link_and_date_options(monkeypatch,tmp_path):
    source=tmp_path/'original.mp4';source.write_bytes(b'video')
    spec=tmp_path/'clips.json';spec.write_text(json.dumps({'source':{'video':str(source)},'clips':[{'id':'a'}]}))
    from sofit import storyboard,render
    calls=[]
    monkeypatch.setattr(storyboard,'add_web_cutaways',lambda *a,**k: calls.append(k) or 0)
    monkeypatch.setattr(render,'render_clips',lambda *a,**k: [])
    assert cli.main(['--render-from',str(spec),'--footage-url',URL,'--footage-after','2026-09-01']) == 0
    assert calls[0]['source_urls'] == (URL,) and calls[0]['published_after'] == '2026-09-01'
    assert cli.main(['--render-from',str(spec),'--footage-after','bad']) == 1
    assert cli.main(['--render-from',str(spec),'--footage-url','file:///secret']) == 1


def test_extractor_isolated_and_bounded(monkeypatch):
    # Restore the real wrapper while mocking the subprocess itself.
    import importlib
    real = importlib.reload(yt)._extract
    monkeypatch.setattr(yt,'available',lambda: True)
    def run(cmd,**kwargs):
        assert '--ignore-config' in cmd and '--no-plugin-dirs' in cmd
        assert '--no-remote-components' in cmd and '--skip-download' in cmd
        assert kwargs['timeout'] == 90 and cmd[-2:] == ['--',URL]
        kwargs['stdout'].write(b'{"title":"test"}')
        return subprocess.CompletedProcess(cmd,0)
    monkeypatch.setattr(yt.subprocess,'run',run)
    assert real(URL) == {'title':'test'}
    def timeout(*a,**k):
        raise subprocess.TimeoutExpired('test',90)
    monkeypatch.setattr(yt.subprocess,'run',timeout)
    with pytest.raises(f.FootageError,match='timed out'):
        real(URL)
    monkeypatch.setattr(yt,'available',lambda: False)
    with pytest.raises(f.FootageError,match='youtube extra'):
        real(URL)


def test_new_link_options_replace_same_automatic_beat(monkeypatch,tmp_path):
    from sofit import storyboard as sb
    video=tmp_path/'cutaway.mp4';video.write_bytes(b'video')
    plan={'span':0,'start':4,'end':8,'source':'web','intent':'robot','query':'robot'}
    doc={'clips':[{'id':'clip','start':0,'end':14,'visual_plan':[plan]}]}
    intents=[]
    def find(intent,**kwargs):
        intents.append(intent)
        return {'video':str(video),'source':{'provider':'youtube'}}
    monkeypatch.setattr(fs,'find_footage',find)
    path=tmp_path/'clips.json'
    assert sb.add_web_cutaways(doc,str(path)) == 1
    assert sb.add_web_cutaways(doc,str(path),source_urls=(URL,),published_after='2026-09-01') == 1
    assert len(doc['clips'][0]['cutaways']) == 1
    assert intents[-1].source_urls == (URL,) and intents[-1].published_after == '2026-09-01'
    assert json.loads(path.read_text())['clips'][0]['visual_plan'][0]['source_urls'] == [URL]
    assert sb.add_web_cutaways(doc,str(path)) == 0
    # New restrictions must not leave a prior auto asset in place after failure.
    monkeypatch.setattr(fs,'find_footage',lambda *a,**k: None)
    assert sb.add_web_cutaways(doc,str(path),published_after='2026-09-20') == 0
    assert doc['clips'][0]['cutaways'] == []


def test_optional_provider_discovery_and_independent_failure(monkeypatch):
    from sofit.footage_commons import CommonsProvider
    searched=[]
    monkeypatch.setattr(CommonsProvider,'search',lambda *a,**k: searched.append('commons') or [])
    monkeypatch.setattr(yt,'available',lambda: True)
    def search(self,*a,**k):
        searched.append(('youtube',self.prefer_recent,self.published_after))
        raise f.FootageError('unavailable')
    monkeypatch.setattr(yt.YouTubeProvider,'search',search)
    intent=f.VisualIntent('robot','robot',4,prefer_recent=True,published_after='2026-09-01')
    assert fs.find_footage(intent) is None
    assert searched == ['commons',('youtube',True,'2026-09-01')]
    searched.clear()
    monkeypatch.setattr(yt,'available',lambda: False)
    assert fs.find_footage(intent) is None and searched == ['commons']


def test_explicit_youtube_link_uses_canonical_identity_and_visual_gate(monkeypatch,tmp_path):
    c=yt.normalize_candidate(info())
    media=tmp_path/'source.media';media.write_bytes(b'video')
    class Cache:
        limits=f.Limits()
        def retrieve(self,candidate):
            return media,{'sha256':'hash','retrieved_at':'2026-09-20T00:00:00+00:00'}
    class Judge:
        cache_key='test'
    resolved=[]
    monkeypatch.setattr(yt,'available',lambda: True)
    monkeypatch.setattr(yt.YouTubeProvider,'resolve',lambda self,url: resolved.append(url) or c)
    monkeypatch.setattr(yt.YouTubeProvider,'subtitles',lambda *a: [])
    inspected=[]
    def select(media,candidate,intent,cues,judge):
        inspected.append(intent.source_urls)
        return fs.SelectedSegment(candidate.source_url,10,14,.9,'visible robot')
    monkeypatch.setattr(fs,'select_segment',select)
    monkeypatch.setattr(fs,'normalize_segment',lambda media,segment,path: path.write_bytes(b'normalized'))
    monkeypatch.setattr(fs,'probe_video',lambda *a: f.VideoInfo(4,1280,720))
    intent=f.VisualIntent('robot','robot',4,source_urls=('https://youtu.be/abcdefghijk',URL+'&t=2'))
    result=fs.find_footage(intent,providers=[],cache=Cache(),judge=Judge())
    assert result and resolved == [URL] and inspected == [(URL,)]
    assert result['source']['published_at'] == '2026-09-18'
    # Rotated media and subtitle URLs cannot force repeated model work.
    c=replace(c,media_url='https://new.googlevideo.com/rotated',original_media_url='https://new.googlevideo.com/rotated',subtitle_urls=())
    assert fs.find_footage(intent,providers=[],cache=Cache(),judge=Judge()) == result
    assert len(inspected) == 1


def test_preferred_publisher_is_resolved_before_news_and_oversized_hits(monkeypatch):
    calls = []
    official = 'https://www.youtube.com/watch?v=official123'
    def extract(target, flat=False):
        if flat:
            return {'entries': [
                {'url': URL, 'title': 'Figure Helix 2.5 demonstration', 'duration': 14000,
                 'channel': 'Figure'},
                *[{'url': 'https://www.youtube.com/watch?v=abcdefghij' + str(i),
                   'title': 'Figure Helix 2.5 demonstration', 'duration': 30, 'channel': 'News'}
                  for i in range(4)],
                {'url': official, 'title': 'Helix 2.5 demonstration', 'duration': 360,
                 'channel': 'Figure'}]}
        calls.append(target)
        return info(webpage_url=target)
    monkeypatch.setattr(yt, '_extract', extract)
    yt.YouTubeProvider(preferred_channels=('Figure',)).search('Figure Helix 2.5')
    assert calls[0] == official and len(calls) == 4
