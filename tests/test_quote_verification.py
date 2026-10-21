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


from crossexam.witness.answer import limit_quote

PARA_64 = ("Regarding POL00055418 (Emails: The Horizon Trial), given that the vast majority of post offices "
           "were understood not to have significant concerns about Horizon, and because of reassurance from "
           "audit risk modelling, feedback from audit, training, support and the perspective of internal IT "
           "specialists, I did not have serious concerns about the system myself, but I would have been "
           "equally keen to be aware of new concerns arising in case this would enable me and my team to "
           "positively support the resolution of something if it became a confirmed issue.")


def test_quote_within_the_limit_is_unchanged():
    assert limit_quote(PARA_17, 40) == PARA_17


def test_long_quote_keeps_whole_sentences_within_the_limit():
    quote = PARA_20  # 2 sentences: 22 + 19 words
    trimmed = limit_quote(quote, 30)
    assert trimmed == quote.split(". ")[0] + "."      # the first sentence only: adding the second would pass 30
    assert len(trimmed.split()) <= 30
    assert quote_matches(trimmed, PARA_20)


def test_single_long_sentence_is_cut_at_a_clause_break_and_still_verifies():
    trimmed = limit_quote(PARA_64, 40)
    assert len(trimmed.split()) <= 40
    assert trimmed.endswith("feedback from audit, training")   # last clause break within 40 words
    assert PARA_64.startswith(trimmed)                          # a verbatim prefix, comma dropped
    assert quote_matches(trimmed, PARA_64)


def test_sentence_with_no_clause_break_is_cut_at_the_limit():
    trimmed = limit_quote("one two three four five six seven eight nine ten", 4)
    assert trimmed == "one two three four"
