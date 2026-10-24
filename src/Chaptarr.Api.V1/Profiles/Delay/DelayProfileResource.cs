using System.Collections.Generic;
using System.Linq;
using Chaptarr.Http.REST;
using NzbDrone.Core.Indexers;
using NzbDrone.Core.Profiles.Delay;

namespace Chaptarr.Api.V1.Profiles.Delay
{
    public class DelayProfileResource : RestResource
    {
        public bool EnableUsenet { get; set; }
        public bool EnableTorrent { get; set; }

        // Nullable so clients that predate direct downloads (e.g. the vendored ABS plugin UI) leave the defaults alone.
        public bool? EnableDirectDownload { get; set; }
        public DownloadProtocol PreferredProtocol { get; set; }
        public int UsenetDelay { get; set; }
        public int TorrentDelay { get; set; }
        public int? DirectDownloadDelay { get; set; }
        public bool BypassIfHighestQuality { get; set; }
        public bool BypassIfAboveCustomFormatScore { get; set; }
        public int MinimumCustomFormatScore { get; set; }
        public int Order { get; set; }
        public HashSet<int> Tags { get; set; }
    }

    public static class DelayProfileResourceMapper
    {
        public static DelayProfileResource ToResource(this DelayProfile model)
        {
            if (model == null)
            {
                return null;
            }

            return new DelayProfileResource
            {
                Id = model.Id,

                EnableUsenet = model.EnableUsenet,
                EnableTorrent = model.EnableTorrent,
                EnableDirectDownload = model.EnableDirectDownload,
                PreferredProtocol = model.PreferredProtocol,
                UsenetDelay = model.UsenetDelay,
                TorrentDelay = model.TorrentDelay,
                DirectDownloadDelay = model.DirectDownloadDelay,
                BypassIfHighestQuality = model.BypassIfHighestQuality,
                BypassIfAboveCustomFormatScore = model.BypassIfAboveCustomFormatScore,
                MinimumCustomFormatScore = model.MinimumCustomFormatScore,
                Order = model.Order,
                Tags = new HashSet<int>(model.Tags)
            };
        }

        public static DelayProfile ToModel(this DelayProfileResource resource)
        {
            if (resource == null)
            {
                return null;
            }

            return new DelayProfile
            {
                Id = resource.Id,

                EnableUsenet = resource.EnableUsenet,
                EnableTorrent = resource.EnableTorrent,
                EnableDirectDownload = resource.EnableDirectDownload ?? true,
                PreferredProtocol = resource.PreferredProtocol,
                UsenetDelay = resource.UsenetDelay,
                TorrentDelay = resource.TorrentDelay,
                DirectDownloadDelay = resource.DirectDownloadDelay ?? 0,
                BypassIfHighestQuality = resource.BypassIfHighestQuality,
                BypassIfAboveCustomFormatScore = resource.BypassIfAboveCustomFormatScore,
                MinimumCustomFormatScore = resource.MinimumCustomFormatScore,
                Order = resource.Order,
                Tags = new HashSet<int>(resource.Tags)
            };
        }

        public static List<DelayProfileResource> ToResource(this IEnumerable<DelayProfile> models)
        {
            return models.Select(ToResource).ToList();
        }
    }
}
