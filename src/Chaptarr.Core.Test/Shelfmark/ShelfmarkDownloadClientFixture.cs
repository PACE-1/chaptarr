using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using NLog;
using NUnit.Framework;
using NzbDrone.Common.Disk;
using NzbDrone.Common.Http;
using NzbDrone.Core.Books;
using NzbDrone.Core.Download;
using NzbDrone.Core.Download.Clients;
using NzbDrone.Core.Download.Clients.Shelfmark;
using NzbDrone.Core.Exceptions;
using NzbDrone.Core.Indexers;
using NzbDrone.Core.Indexers.Shelfmark;
using NzbDrone.Core.Parser.Model;
using NzbDrone.Core.RemotePathMappings;
using ShelfmarkClient = NzbDrone.Core.Download.Clients.Shelfmark.Shelfmark;

namespace Chaptarr.Core.Test.Shelfmark
{
    [TestFixture]
    public class ShelfmarkDownloadClientFixture
    {
        private FakeHttpClient _http;
        private ShelfmarkClient _subject;

        [SetUp]
        public void SetUp()
        {
            var http = DispatchProxy.Create<IHttpClient, FakeHttpClient>();
            _http = (FakeHttpClient)(object)http;

            _subject = new ShelfmarkClient(
                new ShelfmarkProxy(http, LogManager.GetCurrentClassLogger()),
                configService: null,
                diskProvider: null,
                remotePathMappingService: new PrefixRemotePathMappingService(),
                LogManager.GetCurrentClassLogger())
            {
                Definition = new DownloadClientDefinition
                {
                    Id = 4,
                    Name = "Shelfmark",
                    Settings = new ShelfmarkDownloadClientSettings
                    {
                        Host = "shelfmark",
                        Port = 8084,
                        ApiKey = "secret",
                        UserId = 3,
                        Priority = 5
                    }
                }
            };
        }

        [Test]
        public async Task should_queue_the_release_for_the_configured_user()
        {
            _http.Respond(HttpStatusCode.OK, @"{""status"": ""queued"", ""priority"": 5}");

            var id = await _subject.Download(RemoteBookFor(Release()), null);

            Assert.That(id, Is.EqualTo("abc123"), "Shelfmark keys its queue by source_id");

            var request = _http.Requests.Single();
            Assert.That(request.Method, Is.EqualTo(HttpMethod.Post));
            Assert.That(request.Url.FullUri, Is.EqualTo("http://shelfmark:8084/api/releases/download"));
            Assert.That(request.Headers["X-Api-Key"], Is.EqualTo("secret"));

            var body = JObject.Parse(System.Text.Encoding.UTF8.GetString(request.ContentData));
            Assert.That(body.Value<string>("source"), Is.EqualTo("direct_download"));
            Assert.That(body.Value<string>("source_id"), Is.EqualTo("abc123"));
            Assert.That(body.Value<string>("content_type"), Is.EqualTo("ebook"));
            Assert.That(body.Value<int>("on_behalf_of_user_id"), Is.EqualTo(3));
            Assert.That(body.Value<int>("priority"), Is.EqualTo(5));
        }

        [Test]
        public async Task should_treat_an_already_queued_release_as_queued()
        {
            _http.Respond(HttpStatusCode.InternalServerError, @"{""error"": ""Release is already in the download queue""}");

            Assert.That(await _subject.Download(RemoteBookFor(Release()), null), Is.EqualTo("abc123"));
        }

        [Test]
        public void should_reject_a_release_shelfmark_refuses()
        {
            _http.Respond(HttpStatusCode.InternalServerError, @"{""error"": ""Invalid release identifier""}");

            var ex = Assert.ThrowsAsync<DownloadClientRejectedReleaseException>(() => _subject.Download(RemoteBookFor(Release()), null));

            Assert.That(ex.Message, Does.Contain("Invalid release identifier"));
        }

        [Test]
        public void should_report_shelfmark_being_down_as_unavailable()
        {
            _http.Respond(HttpStatusCode.BadGateway, "<html>Bad gateway</html>");

            Assert.ThrowsAsync<DownloadClientUnavailableException>(() => _subject.Download(RemoteBookFor(Release()), null));
        }

        [Test]
        public void should_refuse_releases_from_other_indexers()
        {
            var release = new ReleaseInfo { Title = "Some NZB", DownloadUrl = "https://indexer/api?t=get&id=1", DownloadProtocol = DownloadProtocol.DirectDownload };

            Assert.ThrowsAsync<ReleaseDownloadException>(() => _subject.Download(RemoteBookFor(release), null));
            Assert.That(_http.Requests, Is.Empty);
        }

        [Test]
        public void should_map_the_recorded_queue()
        {
            _http.Respond(HttpStatusCode.OK, ShelfmarkResponses.Status);

            var items = _subject.GetItems().ToDictionary(i => i.DownloadId);

            Assert.That(items.Keys, Is.EquivalentTo(new[] { "q1", "d1", "c1", "c2", "e1" }), "another user's downloads are not shown");

            Assert.That(items["q1"].Status, Is.EqualTo(DownloadItemStatus.Queued));
            Assert.That(items["q1"].Title, Is.EqualTo("Jane Doe - Queued Book"));

            Assert.That(items["d1"].Status, Is.EqualTo(DownloadItemStatus.Downloading));
            Assert.That(items["d1"].MediaType, Is.EqualTo(BookMediaType.Audiobook));
            Assert.That(items["d1"].TotalSize, Is.EqualTo(100L * 1024 * 1024));
            Assert.That(items["d1"].RemainingSize, Is.EqualTo(75L * 1024 * 1024));

            Assert.That(items["c1"].Status, Is.EqualTo(DownloadItemStatus.Completed));
            Assert.That(items["c1"].Title, Is.EqualTo("Jane Doe - Finished Book"));
            Assert.That(items["c1"].OutputPath.FullPath, Is.EqualTo("/data/books/isaac/Finished Book.epub"), "remote path mappings apply");
            Assert.That(items["c1"].RemainingSize, Is.EqualTo(0));
            Assert.That(items["c1"].DownloadClientInfo.Protocol, Is.EqualTo(DownloadProtocol.DirectDownload));

            Assert.That(items["c2"].Status, Is.EqualTo(DownloadItemStatus.Warning), "a finished download with no file cannot be imported");

            Assert.That(items["e1"].Status, Is.EqualTo(DownloadItemStatus.Failed));
            Assert.That(items["e1"].Message, Is.EqualTo("All mirrors failed"));
        }

        [Test]
        public void should_show_every_download_when_no_user_is_set()
        {
            ((ShelfmarkDownloadClientSettings)_subject.Definition.Settings).UserId = null;
            _http.Respond(HttpStatusCode.OK, ShelfmarkResponses.Status);

            var items = _subject.GetItems().ToList();

            Assert.That(items, Has.Count.EqualTo(6));
            Assert.That(items.Single(i => i.DownloadId == "x1").Status, Is.EqualTo(DownloadItemStatus.Failed));
        }

        [Test]
        public void should_cancel_unfinished_downloads_when_removed()
        {
            _http.Respond(HttpStatusCode.NotFound, @"{""error"": ""Failed to cancel download or book not found""}");

            _subject.RemoveItem(new DownloadClientItem { DownloadId = "d 1/x", Status = DownloadItemStatus.Downloading }, false);

            var request = _http.Requests.Single();
            Assert.That(request.Method, Is.EqualTo(HttpMethod.Delete));
            Assert.That(request.Url.FullUri, Is.EqualTo("http://shelfmark:8084/api/download/d%201%2Fx/cancel"));
        }

        [Test]
        public void should_not_call_shelfmark_when_removing_finished_downloads()
        {
            _subject.RemoveItem(new DownloadClientItem { DownloadId = "c1", Status = DownloadItemStatus.Completed }, false);

            Assert.That(_http.Requests, Is.Empty);
        }

        [Test]
        public void should_fail_the_test_for_a_rejected_key()
        {
            _http.Respond(HttpStatusCode.Unauthorized, @"{""error"": ""Unauthorized""}");

            var result = _subject.Test();

            Assert.That(result.IsValid, Is.False);
            Assert.That(result.Errors.Single().PropertyName, Is.EqualTo("ApiKey"));
        }

        [Test]
        public void should_fail_the_test_for_a_key_without_admin_access()
        {
            _http.Respond(HttpStatusCode.OK, "{}");

            var result = _subject.Test();

            Assert.That(result.IsValid, Is.False);
            Assert.That(result.Errors.Single().ErrorMessage, Does.Contain("read-only"));
        }

        [Test]
        public void should_pass_the_test_for_an_admin_key()
        {
            _http.Respond(HttpStatusCode.OK, ShelfmarkResponses.Status);

            Assert.That(_subject.Test().IsValid, Is.True);
        }

        private static ReleaseInfo Release()
        {
            var release = JObject.Parse(@"{""source"": ""direct_download"", ""source_id"": ""abc123"", ""title"": ""Project Hail Mary"", ""format"": ""epub"", ""content_type"": ""ebook"", ""extra"": {""author"": ""Andy Weir""}}");

            return new ReleaseInfo
            {
                Title = "Andy Weir - Project Hail Mary [epub]",
                DownloadUrl = ShelfmarkRelease.BuildDownloadUrl(new ShelfmarkIndexerSettings().BaseUrl, release),
                DownloadProtocol = DownloadProtocol.DirectDownload
            };
        }

        private static RemoteBook RemoteBookFor(ReleaseInfo release)
        {
            return new RemoteBook
            {
                Release = release,
                Author = new Author { Name = "Andy Weir" },
                Books = new List<Book> { new Book { Title = "Project Hail Mary" } }
            };
        }

        public class FakeHttpClient : DispatchProxy
        {
            private readonly Queue<(HttpStatusCode Status, string Content)> _responses = new();

            public List<HttpRequest> Requests { get; } = new();

            public void Respond(HttpStatusCode status, string content)
            {
                _responses.Enqueue((status, content));
            }

            protected override object Invoke(MethodInfo targetMethod, object[] args)
            {
                if (targetMethod?.Name == nameof(IHttpClient.Execute))
                {
                    var request = (HttpRequest)args[0];
                    Requests.Add(request);

                    var (status, content) = _responses.Dequeue();
                    var response = new HttpResponse(request, new HttpHeader { ContentType = "application/json" }, content, status);

                    if (!request.SuppressHttpError && (int)status >= 400)
                    {
                        throw new HttpException(request, response);
                    }

                    return response;
                }

                throw new NotImplementedException($"Test proxy does not implement IHttpClient.{targetMethod?.Name}");
            }
        }

        private class PrefixRemotePathMappingService : IRemotePathMappingService
        {
            public List<RemotePathMapping> All() => throw new NotImplementedException();
            public RemotePathMapping Add(RemotePathMapping mapping) => throw new NotImplementedException();
            public void Remove(int id) => throw new NotImplementedException();
            public RemotePathMapping Get(int id) => throw new NotImplementedException();
            public RemotePathMapping Update(RemotePathMapping mapping) => throw new NotImplementedException();
            public OsPath RemapRemoteToLocal(string host, OsPath remotePath) => RemapRemoteToLocal(0, host, remotePath);
            public OsPath RemapLocalToRemote(string host, OsPath localPath) => localPath;
            public OsPath RemapRemoteToLocal(int downloadClientId, string host, OsPath remotePath)
            {
                return host == "shelfmark" && remotePath.FullPath.StartsWith("/books/", StringComparison.Ordinal)
                    ? new OsPath("/data" + remotePath.FullPath)
                    : remotePath;
            }

            public OsPath RemapLocalToRemote(int downloadClientId, string host, OsPath localPath) => localPath;
            public RemotePathMappingTestResult Test(RemotePathMapping mapping) => throw new NotImplementedException();
        }
    }
}
