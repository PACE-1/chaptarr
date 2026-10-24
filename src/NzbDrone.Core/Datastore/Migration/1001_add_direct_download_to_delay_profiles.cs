using FluentMigrator;
using NzbDrone.Core.Datastore.Migration.Framework;

namespace NzbDrone.Core.Datastore.Migration
{
    // Fork migrations are numbered from 1001 so they never collide with upstream's sequence.
    [Migration(1001)]
    public class add_direct_download_to_delay_profiles : NzbDroneMigrationBase
    {
        protected override void MainDbUpgrade()
        {
            const string tableName = "DelayProfiles";

            if (!Schema.Table(tableName).Column("EnableDirectDownload").Exists())
            {
                Alter.Table(tableName)
                    .AddColumn("EnableDirectDownload")
                    .AsBoolean()
                    .NotNullable()
                    .WithDefaultValue(true);
            }

            if (!Schema.Table(tableName).Column("DirectDownloadDelay").Exists())
            {
                Alter.Table(tableName)
                    .AddColumn("DirectDownloadDelay")
                    .AsInt32()
                    .NotNullable()
                    .WithDefaultValue(0);
            }
        }
    }
}
