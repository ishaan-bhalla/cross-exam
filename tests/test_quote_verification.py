from crossexam.witness.answer import quote_matches

PARA_17 = "I left Post Office Ltd in March 2016."
PARA_20 = ("Document NFSP00000521 (National Federation of Subpostmasters: Report of a meeting of the NEC ) "
           "refers to a crib sheet to assist in disputing Transaction Corrections. I cannot recall this crib "
           "sheet, nor why it would have been felt that such an addition was needed.")
PARA_35 = ('My recollection is that branches would sometimes select "other postage" as a fast way to process '
           "a more complex transaction.")


def test_exact_quote_passes():
    assert quote_matches("I left Post Office Ltd in March 2016.", PARA_17)


def test_quoted_quote_passes():
    assert quote_matches('"I left Post Office Ltd in March 2016."', PARA_17)
    assert quote_matches("“I left Post Office Ltd in March 2016.”", PARA_17)


def test_trailing_full_stop_passes():
    assert quote_matches("I left Post Office Ltd in March 2016", PARA_17)
    assert quote_matches("I left Post Office Ltd.", PARA_17)


def test_truncated_sentence_with_added_full_stop_passes_when_words_are_real():
    assert quote_matches("I cannot recall this crib sheet.", PARA_20)


def test_truncated_sentence_with_added_full_stop_fails_when_words_differ():
    assert not quote_matches("I cannot recall the crib sheet.", PARA_20)
    assert not quote_matches("I cannot recall this sheet.", PARA_20)


def test_changed_word_fails():
    assert not quote_matches("I left Post Office Ltd in April 2016.", PARA_17)
    assert not quote_matches("I joined Post Office Ltd in March 2016.", PARA_17)


def test_single_and_double_quotes_compare_equal():
    assert quote_matches("branches would sometimes select 'other postage' as a fast way", PARA_35)
    assert quote_matches("branches would sometimes select ‘other postage’ as a fast way", PARA_35)


def test_elided_quote_still_verified_in_order():
    assert quote_matches("Document NFSP00000521 ... refers to a crib sheet.", PARA_20)
    assert not quote_matches("refers to a crib sheet ... Document NFSP00000521", PARA_20)


def test_quote_marks_or_punctuation_alone_fail():
    assert not quote_matches('"."', PARA_17)
    assert not quote_matches("...", PARA_17)
