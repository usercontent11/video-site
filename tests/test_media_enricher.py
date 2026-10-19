import json
import unittest

from media_enricher import (
    ExtractionError,
    extract_media_from_html,
    normalize_media_url,
    video_directory,
)


VIDEO_ID = 1193895
PRIMARY_PAGE = "https://mobilexh.live/videos/xhRegression"

THUMB = (
    "https://ic-vt-nss.xhcdn.com/a/"
    "YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/"
    "s(w:526,h:298),webp/001/193/895/526x298.10.webp"
)
IMAGE = (
    "https://ic-vt-nss.cdnsolutions.media/a/"
    "YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/"
    "s(w:1280,h:720),webp/001/193/895/1280x720.10.jpg"
)
PREVIEW = (
    "https://thumb-nss.xhcdn.com/a/98FUYah4CwOMwgXm_08fcw/"
    "001/193/895/526x298.78.t.mp4"
)
SPRITE = (
    "https://thumb-v5.xhcdn.com/a/1CEYcCbBHCk8lcuqtxcXrw/"
    "001/193/895/160x160.50.s.jpg"
)
TRAILER = (
    "https://thumb-v9.cdnsolutions.media/a/98FUYah4CwOMwgXm_08fcw/"
    "001/193/895/526x298.78.t.mp4"
)
FALLBACK = (
    "https://thumb-v5.xhcdn.com/a/FallbackToken123/"
    "001/193/895/526x298.78.t.mp4"
)


def page_html(video_id=VIDEO_ID, include_same_page_card=True, include_other_related=True):
    model = {
        "id": video_id,
        "modelName": "videoModel",
        "title": "Regression fixture",
        "pageURL": PRIMARY_PAGE,
        "thumbURL": THUMB,
        "previewThumbURL": PREVIEW,
        "spriteURL": SPRITE if include_same_page_card else None,
        "trailerURL": TRAILER if include_same_page_card else None,
    }
    cards = []
    if include_same_page_card:
        cards.append({
            "id": VIDEO_ID,
            "videoType": "video",
            "title": "Regression fixture",
            "pageURL": PRIMARY_PAGE,
            "thumbURL": THUMB,
            "imageURL": IMAGE,
            "previewThumbURL": PREVIEW,
            "spriteURL": SPRITE,
            "trailerURL": TRAILER,
            "trailerFallbackUrl": FALLBACK,
            "thumbId": 10,
        })
    if include_other_related:
        cards.append({
            "id": 987654321,
            "videoType": "video",
            "title": "A different related video",
            "pageURL": "https://mobilexh.live/videos/anotherVideo",
            "thumbURL": "https://ic-vt-nss.xhcdn.com/a/wrongToken/"
                       "987/654/321/526x298.41.webp",
            "imageURL": "https://ic-vt-nss.xhcdn.com/a/wrongImage/"
                        "987/654/321/1280x720.41.jpg",
            "previewThumbURL": "https://thumb-nss.xhcdn.com/a/wrongPreview/"
                               "987/654/321/16x9.41.webp",
            "spriteURL": "https://thumb-v5.xhcdn.com/a/wrongSprite/"
                         "987/654/321/526x298.s.webp",
            "trailerURL": "https://thumb-v5.xhcdn.com/a/wrongTrailer/"
                          "987/654/321/526x298.94.t.mp4",
            "trailerFallbackUrl": "https://thumb-v9.xhcdn.com/a/wrongFallback/"
                                  "987/654/321/526x298.94.t.mp4",
            "thumbId": 41,
        })
    state = {
        "videoModel": model,
        "videoEntity": {"id": video_id, "thumbs": {}},
        "relatedVideosComponent": {
            "videoTabInitialData": {
                "videoListProps": {"videoThumbProps": cards}
            }
        },
    }
    return "<html><head><script id='initials-script'>window.initials=" + json.dumps(state) + ";</script></head><body></body></html>"


class MediaExtractorTests(unittest.TestCase):
    def test_video_directory_for_regression_id(self):
        self.assertEqual(video_directory(1193895), "001/193/895")

    def test_video_directory_refuses_unverified_long_ids(self):
        with self.assertRaisesRegex(ValueError, "exceeds the observed nine-digit CDN layout"):
            video_directory(1_234_567_890)

    def test_extracts_only_primary_video_and_preserves_different_cdn_hosts(self):
        result = extract_media_from_html(page_html(), VIDEO_ID, "https://mobilexh.live/videos/1193895")
        self.assertEqual(result.status, "complete")
        self.assertEqual(
            result.fields["image_path"],
            "/a/YWY4Y2RhNzQ4ZTY4ZDYyOGI5MTNlYTJiYjNlOWJmYTE/"
            "s(w:1280,h:720),webp/001/193/895/1280x720.10.jpg",
        )
        self.assertEqual(result.fields["sprite_path"], "/a/1CEYcCbBHCk8lcuqtxcXrw/001/193/895/160x160.50.s.jpg")
        self.assertEqual(result.fields["trailer_path"], "/a/98FUYah4CwOMwgXm_08fcw/001/193/895/526x298.78.t.mp4")
        self.assertEqual(result.fields["trailer_fallback_path"], "/a/FallbackToken123/001/193/895/526x298.78.t.mp4")
        self.assertEqual(result.fields["thumb_id"], 10)
        self.assertEqual(result.fields["trailer_cdn_version"], 9)
        self.assertEqual(result.hosts["image_path"], "ic-vt-nss.cdnsolutions.media")
        self.assertEqual(result.hosts["sprite_path"], "thumb-v5.xhcdn.com")
        self.assertEqual(result.hosts["trailer_path"], "thumb-v9.cdnsolutions.media")
        self.assertEqual(result.source_keys["image_path"], "same_page_videoThumbProps.imageURL")

    def test_never_borrows_media_from_recommendations(self):
        html = page_html(include_same_page_card=False, include_other_related=True)
        result = extract_media_from_html(html, VIDEO_ID, "https://mobilexh.live/videos/1193895")
        self.assertIsNone(result.fields["image_path"])
        self.assertIsNone(result.fields["sprite_path"])
        self.assertIsNone(result.fields["trailer_path"])
        self.assertNotIn("wrongToken", json.dumps(result.fields))
        self.assertEqual(result.status, "partial")

    def test_primary_model_id_must_match_requested_database_id(self):
        with self.assertRaisesRegex(ExtractionError, "does not match requested video ID"):
            extract_media_from_html(page_html(video_id=12345678), VIDEO_ID, "https://mobilexh.live/videos/1193895")

    def test_normalization_keeps_query_parameters_and_encoded_path(self):
        path, host = normalize_media_url(
            "https://thumb-v5.xhcdn.com/a/tokenABC/001/193/895/16x9.10.webp?token=a%2Fb&v=2#client-fragment",
            VIDEO_ID,
            "thumbnail_path",
        )
        self.assertEqual(path, "/a/tokenABC/001/193/895/16x9.10.webp?token=a%2Fb&v=2")
        self.assertEqual(host, "thumb-v5.xhcdn.com")

    def test_rejects_wrong_video_directory_and_untrusted_hosts(self):
        with self.assertRaisesRegex(ValueError, "does not contain video directory"):
            normalize_media_url(
                "https://thumb-v5.xhcdn.com/a/tokenABC/987/654/321/16x9.10.webp",
                VIDEO_ID,
                "thumbnail_path",
            )
        with self.assertRaisesRegex(ValueError, "unexpected media hostname"):
            normalize_media_url(
                "https://evil.example/a/tokenABC/001/193/895/16x9.10.webp",
                VIDEO_ID,
                "thumbnail_path",
            )


if __name__ == "__main__":
    unittest.main()
