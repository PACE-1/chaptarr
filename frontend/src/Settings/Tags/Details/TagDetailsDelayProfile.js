import PropTypes from 'prop-types';
import React from 'react';
import getProtocolName from 'Utilities/String/getProtocolName';
import translate from 'Utilities/String/translate';

function TagDetailsDelayProfile(props) {
  const {
    preferredProtocol,
    enableUsenet,
    enableTorrent,
    enableDirectDownload,
    usenetDelay,
    torrentDelay,
    directDownloadDelay
  } = props;

  return (
    <div>
      <div>
        {translate('TagDetailsProtocol', { protocol: getProtocolName(preferredProtocol) })}
      </div>

      <div>
        {
          enableUsenet ?
            translate('TagDetailsUsenetDelay', { delay: usenetDelay }) :
            translate('TagDetailsUsenetDisabled')
        }
      </div>

      <div>
        {
          enableTorrent ?
            translate('TagDetailsTorrentDelay', { delay: torrentDelay }) :
            translate('TagDetailsTorrentsDisabled')
        }
      </div>

      <div>
        {
          // Missing/null on older profiles means enabled
          enableDirectDownload === false ?
            translate('TagDetailsDirectDownloadDisabled') :
            translate('TagDetailsDirectDownloadDelay', { delay: directDownloadDelay ?? 0 })
        }
      </div>
    </div>
  );
}

TagDetailsDelayProfile.propTypes = {
  preferredProtocol: PropTypes.string.isRequired,
  enableUsenet: PropTypes.bool.isRequired,
  enableTorrent: PropTypes.bool.isRequired,
  enableDirectDownload: PropTypes.bool,
  usenetDelay: PropTypes.number.isRequired,
  torrentDelay: PropTypes.number.isRequired,
  directDownloadDelay: PropTypes.number
};

export default TagDetailsDelayProfile;
