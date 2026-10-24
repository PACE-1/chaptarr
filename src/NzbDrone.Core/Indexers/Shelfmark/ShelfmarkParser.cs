using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using NzbDrone.Common.Extensions;
using NzbDrone.Core.Indexers.Exceptions;
using NzbDrone.Core.Parser.Model;

namespace NzbDrone.Core.Indexers.Shelfmark
{
    public class ShelfmarkParser : IParseIndexerResponse
    {
        private static readonly Regex SizeRegex = new Regex(@"^\s*(?<value>\d+(?:[.,]\d+)?)\s*(?<unit>[KMGT]?i?B|bytes?)?\s*$",
            RegexOptions.IgnoreCase | RegexOptions.Compiled);

        public ShelfmarkIndexerSettings Settings { get; set; }

        public IList<ReleaseInfo> ParseResponse(IndexerResponse indexerResponse)
        {
            var response = indexerResponse.HttpResponse;

            if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            {
                throw new ApiKeyException("Shelfmark rejected the API key");
            }

            JObject body;

            try
            {
                body = JObject.Parse(response.Content);
            }
            catch (Exception ex)
            {
                throw new IndexerException(indexerResponse, "Shelfmark returned something that is not JSON: " + ex.Message);
            }

            if (response.StatusCode != HttpStatusCode.OK)
            {
                var error = body.Value<string>("error") ?? response.StatusCode.ToString();

                // 503 means every searched source failed or is unavailable, which is an empty result rather than a broken indexer.
                if (response.StatusCode == HttpStatusCode.ServiceUnavailable)
                {
                    return new List<ReleaseInfo>();
                }

                throw new IndexerException(indexerResponse, "Shelfmark search failed: " + error);
            }

            var request = indexerResponse.Request as ShelfmarkIndexerRequest;
            var releases = body["releases"] as JArray ?? new JArray();

            return releases.OfType<JObject>()
                .Select(r => ToReleaseInfo(r, request))
                .Where(r => r != null)
                .ToList();
        }

        private ReleaseInfo ToReleaseInfo(JObject release, ShelfmarkIndexerRequest request)
        {
            var source = release.Value<string>("source");
            var sourceId = release.Value<string>("source_id");
            var title = release.Value<string>("title");

            if (source.IsNullOrWhiteSpace() || sourceId.IsNullOrWhiteSpace() || title.IsNullOrWhiteSpace())
            {
                return null;
            }

            var contentType = release.Value<string>("content_type").IsNotNullOrWhiteSpace()
                ? release.Value<string>("content_type")
                : request?.ContentType ?? "ebook";

            // Shelfmark infers the content type from the source when it is missing, so always send the one searched for.
            release["content_type"] = contentType;

            var format = release.Value<string>("format");
            var author = release["extra"]?.Value<string>("author") ?? request?.Author;

            return new ReleaseInfo
            {
                Guid = $"Shelfmark-{source}-{sourceId}",
                Title = BuildTitle(title, author, format, contentType),
                Size = GetSize(release),
                DownloadUrl = ShelfmarkRelease.BuildDownloadUrl(Settings.BaseUrl, release),
                InfoUrl = release.Value<string>("info_url"),
                DownloadProtocol = DownloadProtocol.DirectDownload,
                PublishDate = DateTime.UtcNow,
                Source = release.Value<string>("indexer") ?? source,
                Container = format
            };
        }

        // Chaptarr parses author, title and format from the release title, while Shelfmark's
        // title is often the bare book title with the format in a separate field.
        public static string BuildTitle(string title, string author, string format, string contentType)
        {
            var result = title.Trim();

            if (author.IsNotNullOrWhiteSpace() && !result.Contains(author.Trim(), StringComparison.OrdinalIgnoreCase))
            {
                result = $"{author.Trim()} - {result}";
            }

            var hasFormat = format.IsNotNullOrWhiteSpace() &&
                            Regex.IsMatch(result, $@"\b{Regex.Escape(format.Trim())}\b", RegexOptions.IgnoreCase);

            if (format.IsNotNullOrWhiteSpace() && !hasFormat)
            {
                result = $"{result} [{format.Trim().ToLowerInvariant()}]";
            }
            else if (format.IsNullOrWhiteSpace() && contentType == "audiobook" &&
                     !result.Contains("audiobook", StringComparison.OrdinalIgnoreCase))
            {
                result = $"{result} [audiobook]";
            }

            return result;
        }

        public static long GetSize(JObject release)
        {
            var bytes = release.Value<long?>("size_bytes");

            if (bytes.HasValue && bytes.Value > 0)
            {
                return bytes.Value;
            }

            return ParseSize(release.Value<string>("size"));
        }

        public static long ParseSize(string size)
        {
            if (size.IsNullOrWhiteSpace())
            {
                return 0;
            }

            var match = SizeRegex.Match(size);

            if (!match.Success ||
                !double.TryParse(match.Groups["value"].Value.Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var value))
            {
                return 0;
            }

            var unit = match.Groups["unit"].Value.ToUpperInvariant();
            var multiplier = unit.Length > 0 ? unit[0] switch
            {
                'K' => 1024L,
                'M' => 1024L * 1024,
                'G' => 1024L * 1024 * 1024,
                'T' => 1024L * 1024 * 1024 * 1024,
                _ => 1L
            } : 1L;

            return (long)(value * multiplier);
        }
    }
}
