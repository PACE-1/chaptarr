using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NLog;
using NzbDrone.Common.Extensions;
using NzbDrone.Common.Http;

namespace NzbDrone.Core.Download.Clients.Shelfmark
{
    public interface IShelfmarkProxy
    {
        void QueueRelease(JObject release, ShelfmarkDownloadClientSettings settings);
        List<ShelfmarkTask> GetTasks(ShelfmarkDownloadClientSettings settings);
        void CancelDownload(string id, ShelfmarkDownloadClientSettings settings);
        void TestConnection(ShelfmarkDownloadClientSettings settings);
    }

    public class ShelfmarkProxy : IShelfmarkProxy
    {
        private const string AlreadyQueuedMessage = "already in the download queue";

        private readonly IHttpClient _httpClient;
        private readonly Logger _logger;

        public ShelfmarkProxy(IHttpClient httpClient, Logger logger)
        {
            _httpClient = httpClient;
            _logger = logger;
        }

        public void QueueRelease(JObject release, ShelfmarkDownloadClientSettings settings)
        {
            var body = (JObject)release.DeepClone();

            body["priority"] = settings.Priority;

            if (settings.UserId.HasValue)
            {
                body["on_behalf_of_user_id"] = settings.UserId.Value;
            }
            else
            {
                body.Remove("on_behalf_of_user_id");
            }

            var request = BuildRequest("/api/releases/download", settings).Post().Build();
            request.Headers.ContentType = "application/json";
            request.SetContent(body.ToString(Formatting.None));

            var response = Execute(request);

            if (response.StatusCode == HttpStatusCode.OK)
            {
                return;
            }

            var error = ReadError(response);

            // Queuing is keyed by source_id, so a release Shelfmark already holds is the download Chaptarr asked for.
            if (error.Contains(AlreadyQueuedMessage, StringComparison.OrdinalIgnoreCase))
            {
                _logger.Debug("Shelfmark already has '{0}' queued", release.Value<string>("title"));
                return;
            }

            // Shelfmark answers a release it cannot queue (policy, unavailable source, bad id) with 4xx/500 and a reason;
            // a gateway in front of it answers 502-504 when Shelfmark itself is down.
            if (response.StatusCode is HttpStatusCode.BadGateway or HttpStatusCode.ServiceUnavailable or HttpStatusCode.GatewayTimeout)
            {
                throw new DownloadClientUnavailableException("Shelfmark is unavailable: {0}", error);
            }

            throw new ShelfmarkRejectedException(error);
        }

        public List<ShelfmarkTask> GetTasks(ShelfmarkDownloadClientSettings settings)
        {
            var request = BuildRequest("/api/status", settings).Build();
            var response = Execute(request);

            if (response.StatusCode != HttpStatusCode.OK)
            {
                throw new DownloadClientException("Unable to read Shelfmark's queue: {0}", ReadError(response));
            }

            var status = JObject.Parse(response.Content);
            var tasks = new List<ShelfmarkTask>();

            // The status is grouped by queue state: {"queued": {id: task}, "downloading": {...}, ...}.
            foreach (var group in status.Properties())
            {
                if (group.Value is not JObject entries)
                {
                    continue;
                }

                foreach (var entry in entries.Properties())
                {
                    if (entry.Value is not JObject value)
                    {
                        continue;
                    }

                    var task = value.ToObject<ShelfmarkTask>();
                    task.Id ??= entry.Name;
                    task.Status ??= group.Name;
                    tasks.Add(task);
                }
            }

            return tasks;
        }

        public void CancelDownload(string id, ShelfmarkDownloadClientSettings settings)
        {
            var request = BuildRequest($"/api/download/{Uri.EscapeDataString(id)}/cancel", settings).Build();
            request.Method = HttpMethod.Delete;

            var response = Execute(request);

            // 404 means Shelfmark no longer holds the task, which is what removing it wants anyway.
            if (response.StatusCode != HttpStatusCode.OK && response.StatusCode != HttpStatusCode.NotFound)
            {
                throw new DownloadClientException("Unable to cancel the Shelfmark download: {0}", ReadError(response));
            }
        }

        public void TestConnection(ShelfmarkDownloadClientSettings settings)
        {
            var request = BuildRequest("/api/status", settings).Build();
            var response = Execute(request);

            if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            {
                throw new DownloadClientAuthenticationException("Shelfmark rejected the API key");
            }

            if (response.StatusCode != HttpStatusCode.OK)
            {
                throw new DownloadClientException("Shelfmark answered {0}: {1}", (int)response.StatusCode, ReadError(response));
            }

            // A non-admin session gets an empty object here, which means the key did not grant admin access.
            if (response.Content.Trim() == "{}")
            {
                throw new DownloadClientAuthenticationException("Shelfmark did not treat the API key as admin. Use SHELFMARK_API_KEY, not the read-only key.");
            }
        }

        private HttpRequestBuilder BuildRequest(string resource, ShelfmarkDownloadClientSettings settings)
        {
            var baseUrl = HttpRequestBuilder.BuildBaseUrl(settings.UseSsl, settings.Host, settings.Port, settings.UrlBase);

            return new HttpRequestBuilder(baseUrl)
                .Resource(resource)
                .Accept(HttpAccept.Json)
                .SetHeader("X-Api-Key", settings.ApiKey);
        }

        private HttpResponse Execute(HttpRequest request)
        {
            request.SuppressHttpError = true;

            try
            {
                var response = _httpClient.Execute(request);

                if (response.StatusCode is HttpStatusCode.Unauthorized)
                {
                    throw new DownloadClientAuthenticationException("Shelfmark rejected the API key");
                }

                return response;
            }
            catch (HttpRequestException ex)
            {
                throw new DownloadClientUnavailableException("Unable to connect to Shelfmark, {0}", ex, ex.Message);
            }
            catch (WebException ex)
            {
                throw new DownloadClientUnavailableException("Unable to connect to Shelfmark, {0}", ex, ex.Message);
            }
        }

        private static string ReadError(HttpResponse response)
        {
            try
            {
                var error = JObject.Parse(response.Content).Value<string>("error");

                if (error.IsNotNullOrWhiteSpace())
                {
                    return error;
                }
            }
            catch (JsonException)
            {
            }

            return $"HTTP {(int)response.StatusCode}";
        }
    }

    public class ShelfmarkRejectedException : DownloadClientException
    {
        public ShelfmarkRejectedException(string reason)
            : base("Shelfmark refused the download: " + reason)
        {
            Reason = reason;
        }

        public string Reason { get; }
    }
}
