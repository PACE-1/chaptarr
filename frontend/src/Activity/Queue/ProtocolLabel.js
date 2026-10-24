import PropTypes from 'prop-types';
import React from 'react';
import Label from 'Components/Label';
import styles from './ProtocolLabel.css';

function ProtocolLabel({ protocol }) {
  let protocolName = protocol;

  if (protocol === 'usenet') {
    protocolName = 'nzb';
  } else if (protocol === 'directDownload') {
    protocolName = 'direct';
  }

  return (
    <Label className={styles[protocol]}>
      {protocolName}
    </Label>
  );
}

ProtocolLabel.propTypes = {
  protocol: PropTypes.string.isRequired
};

export default ProtocolLabel;
