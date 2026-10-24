using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using FluentValidation.Results;
using NLog;
using NzbDrone.Common.Disk;
using NzbDrone.Common.Extensions;
using NzbDrone.Core.Books;
using NzbDrone.Core.Configuration;
using NzbDrone.Core.Exceptions;
using NzbDrone.Core.Indexers;
using NzbDrone.Core.Indexers.Shelfmark;
using NzbDrone.Core.Parser.Model;
using NzbDrone.Core.RemotePathMappings;
using NzbDrone.Core.Validation;

namespace NzbDrone.Core.Download.Clients.Shelfmark
{
    public class Shelfmark : DownloadClientBase<ShelfmarkDownloadClientSettings>
    {
        private readonly IShelfmarkProxy _proxy;

        public Shelfmark(IShelfmarkProxy proxy,
                         IConfigService configService,
                         IDiskProvider diskProvider,
                         IRemotePathMappingService remotePathMappingService,
                         Logger logger)
            : base(configService, diskProvider, remotePathMappingService, logger)
        {
            _proxy = proxy;
        }

        public override string Name => "Shelfmark";

        public override DownloadProtocol Protocol => DownloadProtocol.DirectDownload;

        public override Task<string> Download(RemoteBook remoteBook, IIndexer indexer)
        {
            var release = ParseRelease(remoteBook.Release);

            try
            {
                _proxy.QueueRelease(release, Settings);
            }
            catch (ShelfmarkRejectedException ex)
            {
                throw new DownloadClientRejectedReleaseException(remoteBook.Release, ex.Message, ex);
            }

            _logger.Info("Queued '{0}' in Shelfmark{1}", remoteBook.Release.Title, Settings.UserId.HasValue ? $" for user {Settings.UserId}" : string.Empty);

            return Task.FromResult(ShelfmarkRelease.GetTaskId(release));
        }

        public override IEnumerable<DownloadClientItem> GetItems()
        {
            var tasks = _proxy.GetTasks(Settings);

            foreach (var task in tasks)
            {
                if (task.Id.IsNullOrWhiteSpace())
                {
                    continue;
                }

                // One Shelfmark serves every user, so only show this instance's user's downloads.
                if (Settings.UserId.HasValue && task.UserId != Settings.UserId)
                {
                    continue;
                }

                yield return ToDownloadClientItem(task);
            }
        }

        public override void RemoveItem(DownloadClientItem item, bool deleteData)
        {
            if (item.Status is not (DownloadItemStatus.Completed or DownloadItemStatus.Failed))
            {
                _proxy.CancelDownload(item.DownloadId, Settings);
            }

            if (deleteData)
            {
                DeleteItemData(item);
            }
        }

        public override DownloadClientInfo GetStatus()
        {
            return new DownloadClientInfo
            {
                IsLocalhost = Settings.Host == "127.0.0.1" || Settings.Host == "localhost"
            };
        }

        protected override void Test(List<ValidationFailure> failures)
        {
            try
            {
                _proxy.TestConnection(Settings);
            }
            catch (DownloadClientAuthenticationException ex)
            {
                _logger.Error(ex, ex.Message);
                failures.Add(new ValidationFailure("ApiKey", ex.Message));
                return;
            }
            catch (Exception ex)
            {
                _logger.Error(ex, "Unable to test Shelfmark");
                failures.Add(new NzbDroneValidationFailure("Host", "Unable to connect to Shelfmark")
                {
                    DetailedDescription = ex.Message
                });
                return;
            }
        }

        private DownloadClientItem ToDownloadClientItem(ShelfmarkTask task)
        {
            var totalSize = ShelfmarkParser.ParseSize(task.Size);
            var progress = Math.Clamp(task.Progress ?? 0, 0, 100);
            var status = GetStatus(task);

            var item = new DownloadClientItem
            {
                DownloadClientInfo = DownloadClientItemClientInfo.FromDownloadClient(this, false),
                DownloadId = task.Id,
                Title = task.Author.IsNotNullOrWhiteSpace() && task.Title.IsNotNullOrWhiteSpace() && !task.Title.Contains(task.Author, StringComparison.OrdinalIgnoreCase)
                    ? $"{task.Author} - {task.Title}"
                    : task.Title ?? task.Id,
                MediaType = task.ContentType.IsNullOrWhiteSpace()
                    ? null
                    : task.ContentType.Contains("audio", StringComparison.OrdinalIgnoreCase) ? BookMediaType.Audiobook : BookMediaType.Ebook,
                TotalSize = totalSize,
                RemainingSize = status == DownloadItemStatus.Completed ? 0 : (long)(totalSize * (100 - progress) / 100),
                Status = status,
                Message = task.StatusMessage,
                CanMoveFiles = true,
                CanBeRemoved = true
            };

            if (task.DownloadPath.IsNotNullOrWhiteSpace())
            {
                item.OutputPath = RemapRemoteToLocal(new OsPath(task.DownloadPath));
            }
            else if (status == DownloadItemStatus.Completed)
            {
                // Shelfmark clears download_path when the file is gone or was sent elsewhere (email, browser).
                item.Status = DownloadItemStatus.Warning;
                item.Message = "Shelfmark finished this download but no longer has a file for it to import. Check Shelfmark's output mode and destination.";
            }

            return item;
        }

        private static DownloadItemStatus GetStatus(ShelfmarkTask task)
        {
            switch (task.Status?.ToLowerInvariant())
            {
                case "downloading":
                    return DownloadItemStatus.Downloading;
                case "complete":
                    return DownloadItemStatus.Completed;
                case "error":
                case "cancelled":
                    return DownloadItemStatus.Failed;
                default:
                    // queued, resolving, locating
                    return DownloadItemStatus.Queued;
            }
        }

        private static Newtonsoft.Json.Linq.JObject ParseRelease(ReleaseInfo release)
        {
            try
            {
                return ShelfmarkRelease.ParseDownloadUrl(release.DownloadUrl);
            }
            catch (Exception ex) when (ex is FormatException or Newtonsoft.Json.JsonException)
            {
                throw new ReleaseDownloadException(release, "This release did not come from a Shelfmark indexer, so Shelfmark cannot download it", ex);
            }
        }
    }
}
