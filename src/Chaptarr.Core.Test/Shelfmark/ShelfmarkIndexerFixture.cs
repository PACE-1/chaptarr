using System.Collections.Generic;
using System.Linq;
using System.Net;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using NzbDrone.Common.Http;
using NzbDrone.Core.Books;
using NzbDrone.Core.Indexers;
using NzbDrone.Core.Indexers.Exceptions;
using NzbDrone.Core.Indexers.Shelfmark;
using NzbDrone.Core.IndexerSearch.Definitions;

namespace Chaptarr.Core.Test.Shelfmark
{
    [TestFixture]
    public class ShelfmarkIndexerFixture
    {
        private ShelfmarkIndexerSettings _settings;

        [SetUp]
        public void SetUp()
        {
            _settings = new ShelfmarkIndexerSettings
            {
                BaseUrl = "http://shelfmark:8084/",
                ApiKey = "secret"
            };
        }

        private IList<NzbDrone.Core.Parser.Model.ReleaseInfo> Parse(string content, HttpStatusCode status = HttpStatusCode.OK, string contentType = "ebook")
        {
            var request = new HttpRequest("http://shelfmark:8084/api/releases");
            var response = new HttpResponse(request, new HttpHeader { ContentType = "application/json" }, content, status);
            var indexerRequest = new ShelfmarkIndexerRequest(request, "Andy Weir", contentType);

            return new ShelfmarkParser { Settings = _settings }.ParseResponse(new IndexerResponse(indexerRequest, response));
        }

        [Test]
        public void should_parse_recorded_releases_as_direct_downloads()
        {
            var releases = Parse(ShelfmarkResponses.Releases);

            Assert.That(releases, Has.Count.EqualTo(2), "a release without a source_id cannot be queued, so it is dropped");
            Assert.That(releases.All(r => r.DownloadProtocol == DownloadProtocol.DirectDownload), Is.True);

            var ebook = releases[0];
            Assert.That(ebook.Title, Is.EqualTo("Andy Weir - Project Hail Mary [epub]"));
            Assert.That(ebook.Guid, Is.EqualTo("Shelfmark-direct_download-5c1b6f0e2f6a4b8e9d0c1a2b3c4d5e6f"));
            Assert.That(ebook.Size, Is.EqualTo((long)(2.4 * 1024 * 1024)));
            Assert.That(ebook.InfoUrl, Does.StartWith("https://annas-archive.example/"));
            Assert.That(ebook.DownloadUrl, Does.StartWith("http://shelfmark:8084/api/releases/download?release="));

            var audiobook = releases[1];
            Assert.That(audiobook.Title, Is.EqualTo("Andy Weir - Project Hail Mary (2021) [Ray Porter] M4B"), "titles that already name the author and format are kept as they are");
            Assert.That(audiobook.Size, Is.EqualTo(1181116006));
            Assert.That(audiobook.Source, Is.EqualTo("MyAnonamouse"));
        }

        [Test]
        public void should_carry_the_whole_release_to_the_download_client()
        {
            var ebook = Parse(ShelfmarkResponses.Releases).First();

            var release = ShelfmarkRelease.ParseDownloadUrl(ebook.DownloadUrl);

            Assert.That(release.Value<string>("source"), Is.EqualTo("direct_download"));
            Assert.That(ShelfmarkRelease.GetTaskId(release), Is.EqualTo("5c1b6f0e2f6a4b8e9d0c1a2b3c4d5e6f"));
            Assert.That(release.Value<string>("content_type"), Is.EqualTo("ebook"), "the searched content type is filled in when Shelfmark left it empty");
            Assert.That(release["extra"]?.Value<string>("publisher"), Is.EqualTo("Ballantine"));
        }

        [Test]
        public void should_keep_the_content_type_shelfmark_reported()
        {
            var audiobook = Parse(ShelfmarkResponses.Releases, contentType: "ebook")[1];

            Assert.That(ShelfmarkRelease.ParseDownloadUrl(audiobook.DownloadUrl).Value<string>("content_type"), Is.EqualTo("audiobook"));
        }

        [Test]
        public void should_treat_all_sources_failing_as_no_results()
        {
            Assert.That(Parse(@"{""error"": ""Anna's Archive is unavailable""}", HttpStatusCode.ServiceUnavailable), Is.Empty);
        }

        [Test]
        public void should_report_a_rejected_api_key()
        {
            Assert.Throws<ApiKeyException>(() => Parse(@"{""error"": ""Unauthorized""}", HttpStatusCode.Unauthorized));
        }

        [Test]
        public void should_report_other_search_errors()
        {
            var ex = Assert.Throws<IndexerException>(() => Parse(@"{""error"": ""Unknown metadata provider: hardcover""}", HttpStatusCode.BadRequest));

            Assert.That(ex.Message, Does.Contain("Unknown metadata provider"));
        }

        [Test]
        public void should_round_trip_unicode_releases()
        {
            var original = JObject.Parse(@"{""source"": ""irc"", ""source_id"": ""ü/+=?&"", ""title"": ""Den nakna solen — Asimov""}");

            var url = ShelfmarkRelease.BuildDownloadUrl("http://shelfmark", original);
            var parsed = ShelfmarkRelease.ParseDownloadUrl(url);

            var payload = url.Substring(url.IndexOf("release=", System.StringComparison.Ordinal) + "release=".Length);
            Assert.That(payload, Does.Match("^[A-Za-z0-9_-]+$"), "the payload is URL safe");
            Assert.That(JToken.DeepEquals(parsed, original), Is.True);
        }

        [Test]
        public void should_reject_urls_that_are_not_shelfmark_releases()
        {
            Assert.That(ShelfmarkRelease.IsShelfmarkDownloadUrl("https://indexer/api?t=get&id=1"), Is.False);
            Assert.Throws<System.FormatException>(() => ShelfmarkRelease.ParseDownloadUrl("https://indexer/api?t=get&id=1"));
        }

        [TestCase("2.4MB", 2516582)]
        [TestCase("1,5 GB", 1610612736)]
        [TestCase("512 KiB", 524288)]
        [TestCase("1234", 1234)]
        [TestCase("", 0)]
        [TestCase("unknown", 0)]
        public void should_parse_sizes(string size, long expected)
        {
            Assert.That(ShelfmarkParser.ParseSize(size), Is.EqualTo(expected));
        }

        [TestCase("Project Hail Mary", "Andy Weir", null, "audiobook", "Andy Weir - Project Hail Mary [audiobook]")]
        [TestCase("Project Hail Mary", null, "pdf", "ebook", "Project Hail Mary [pdf]")]
        [TestCase("Project Hail Mary.epub", "Andy Weir", "epub", "ebook", "Andy Weir - Project Hail Mary.epub")]
        public void should_build_titles_chaptarr_can_parse(string title, string author, string format, string contentType, string expected)
        {
            Assert.That(ShelfmarkParser.BuildTitle(title, author, format, contentType), Is.EqualTo(expected));
        }

        [Test]
        public void should_search_by_title_and_author_for_the_requested_media_type()
        {
            var criteria = new BookSearchCriteria
            {
                Author = new Author { Name = "Andy Weir" },
                BookTitle = "Project Hail Mary",
                Books = new List<Book> { new Book { Id = 12, Title = "Project Hail Mary", MediaType = BookMediaType.Audiobook, HardcoverBookId = "hc-1" } }
            };

            var requests = Requests(criteria);

            Assert.That(requests, Has.Count.EqualTo(1));
            var url = requests[0].Url.FullUri;
            Assert.That(url, Does.StartWith("http://shelfmark:8084/api/releases?"));
            Assert.That(url, Does.Contain("provider=manual").And.Contain("book_id=chaptarr-12").And.Contain("content_type=audiobook"));
            Assert.That(url, Does.Contain("title=Project%20Hail%20Mary").And.Contain("author=Andy%20Weir"));
            Assert.That(requests[0].HttpRequest.Headers["X-Api-Key"], Is.EqualTo("secret"));
            Assert.That(requests[0].HttpRequest.RequestTimeout.TotalSeconds, Is.EqualTo(120));
        }

        [Test]
        public void should_search_each_configured_source_and_by_hardcover_id_when_enabled()
        {
            _settings.Sources = "direct_download, prowlarr";
            _settings.UseHardcoverIds = true;

            var criteria = new BookSearchCriteria
            {
                Author = new Author { Name = "Andy Weir" },
                BookTitle = "Project Hail Mary",
                Books = new List<Book> { new Book { Id = 12, Title = "Project Hail Mary", MediaType = BookMediaType.Ebook, HardcoverBookId = "hc-1" } }
            };

            var urls = Requests(criteria).Select(r => r.Url.FullUri).ToList();

            Assert.That(urls, Has.Count.EqualTo(2));
            Assert.That(urls.All(u => u.Contains("provider=hardcover") && u.Contains("book_id=hc-1") && u.Contains("content_type=ebook")), Is.True);
            Assert.That(urls[0], Does.Contain("source=direct_download"));
            Assert.That(urls[1], Does.Contain("source=prowlarr"));
        }

        [Test]
        public void should_search_both_media_types_when_the_search_is_mixed()
        {
            var criteria = new BookSearchCriteria
            {
                Author = new Author { Name = "Andy Weir" },
                BookTitle = "Project Hail Mary",
                Books = new List<Book>
                {
                    new Book { Id = 12, Title = "Project Hail Mary", MediaType = BookMediaType.Ebook },
                    new Book { Id = 13, Title = "Project Hail Mary", MediaType = BookMediaType.Audiobook }
                }
            };

            var urls = Requests(criteria).Select(r => r.Url.FullUri).ToList();

            Assert.That(urls.Count(u => u.Contains("content_type=ebook")), Is.EqualTo(1));
            Assert.That(urls.Count(u => u.Contains("content_type=audiobook")), Is.EqualTo(1));
        }

        [Test]
        public void should_cap_author_searches()
        {
            var books = Enumerable.Range(1, 40)
                .Select(i => new Book { Id = i, Title = "Book " + i, MediaType = BookMediaType.Ebook })
                .ToList();

            var chain = new ShelfmarkRequestGenerator { Settings = _settings }.GetSearchRequests(new AuthorSearchCriteria
            {
                Author = new Author { Name = "Andy Weir" },
                Books = books
            });

            Assert.That(chain.GetAllTiers().SelectMany(t => t).Count(), Is.EqualTo(ShelfmarkRequestGenerator.MaxBooksPerAuthorSearch));
        }

        [Test]
        public void should_not_offer_rss()
        {
            Assert.That(new ShelfmarkRequestGenerator { Settings = _settings }.GetRecentRequests().GetAllTiers().SelectMany(t => t), Is.Empty);
        }

        private List<IndexerRequest> Requests(BookSearchCriteria criteria)
        {
            return new ShelfmarkRequestGenerator { Settings = _settings }
                .GetSearchRequests(criteria)
                .GetAllTiers()
                .SelectMany(t => t)
                .ToList();
        }
    }
}
