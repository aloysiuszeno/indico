// This file is part of Indico.
// Copyright (C) 2002 - 2019 CERN
//
// Indico is free software; you can redistribute it and/or
// modify it under the terms of the MIT License; see the
// LICENSE file for more details.

import React from 'react';
import PropTypes from 'prop-types';
import {useDispatch, useSelector} from 'react-redux';
import {Form as FinalForm} from 'react-final-form';
import {Form} from 'semantic-ui-react';

import {FinalSubmitButton, FinalTextArea} from 'indico/react/forms';
import {Translate} from 'indico/react/i18n';

import {EditingReviewAction} from '../../../models';
import {reviewEditable} from '../../../actions';
import {getLastRevision} from '../../../selectors';

export default function RequestChangesForm({setLoading, onSuccess}) {
  const dispatch = useDispatch();
  const lastRevision = useSelector(getLastRevision);
  const requestChanges = async formData => {
    setLoading(true);
    const rv = await dispatch(
      reviewEditable(lastRevision, {
        ...formData,
        action: EditingReviewAction.requestUpdate,
      })
    );

    setLoading(false);
    if (rv.error) {
      return rv.error;
    }

    onSuccess();
  };

  return (
    <FinalForm onSubmit={requestChanges} subscription={{}} initialValues={{comment: ''}}>
      {fprops => (
        <Form onSubmit={fprops.handleSubmit}>
          <FinalTextArea
            name="comment"
            placeholder={Translate.string('Leave a comment...')}
            hideValidationError
            autoFocus
          />
          <div style={{display: 'flex', justifyContent: 'flex-end'}}>
            <FinalSubmitButton label={Translate.string('Submit')} />
          </div>
        </Form>
      )}
    </FinalForm>
  );
}

RequestChangesForm.propTypes = {
  setLoading: PropTypes.func.isRequired,
  onSuccess: PropTypes.func.isRequired,
};
