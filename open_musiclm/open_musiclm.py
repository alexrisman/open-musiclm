import itertools
from dataclasses import dataclass
from typing import List, Optional, Union

import torch
import torch.nn.functional as F
import tqdm
from audiolm_pytorch import (CoarseTransformer, CoarseTransformerWrapper,
                             FairseqVQWav2Vec, FineTransformer,
                             FineTransformerWrapper, HubertWithKmeans,
                             SemanticTransformer, SemanticTransformerWrapper,
                             SoundStream)
from audiolm_pytorch.hubert_kmeans import HubertWithKmeans
from audiolm_pytorch.t5 import DEFAULT_T5_NAME
from audiolm_pytorch.vq_wav2vec import FairseqVQWav2Vec
from beartype import beartype
from beartype.typing import List, Optional, Union
from clap_quantized import ClapQuantized
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange
from torch import einsum, nn
from transformer import Transformer
from utils import (all_rows_have_eos_id, append_eos_id,
                   batch_unique_consecutive, ceil_div, default, eval_decorator,
                   exists, generate_mask_with_prob, gumbel_sample,
                   mask_out_after_eos_id, round_down_nearest_multiple, top_k)


@dataclass
class TokenSequence():
    """
    Information about a type of token sequence that the TokenConditionedTransformer handles
    e.g. semantic tokens, coarse acoustic tokens, fine acoustic tokens, etc.
    """
    name: Optional[str]
    vocab_size: int         # i.e. codebook_size

    tokens_per_step: int    # e.g. 1 for semantic, Q for coarse acoustic, ...
    sequence_length: int    # e.g. 12 for semantic, n_time_steps for coarse acoustic, ...


class TokenConditionedTransformer(nn.Module):
    """
    Combination of the SemanticTransformer, CoarseTransformer and FineTransformer in lucidrain's AudioLM implementation,
    except that it is not tied to any specific type of token sequence. Instead, it can handle a variable number of
    token sequences, each with its own vocab size, tokens_per_step and sequence length.
    https://github.com/lucidrains/audiolm-pytorch/blob/main/audiolm_pytorch/audiolm_pytorch.py
    """
    # TODO: Add in text conditioning for parity with AudioLM. Not important for MusicLM though.

    def __init__(
        self,
        *,
        token_sequences: List[TokenSequence],
        dim,
        depth,
        heads = 8,
        attn_dropout = 0.,
        ff_dropout = 0.,
        has_condition = False,
        # t5_name = DEFAULT_T5_NAME,
        cond_as_self_attn_prefix = False,
        cond_drop_prob = 0.5,
        grad_shrink_alpha = 0.1,
        **kwargs
    ):
        super().__init__()

        self.token_sequences = token_sequences

        self.has_condition = has_condition
        self.cond_drop_prob = cond_drop_prob
        
        self.start_tokens, self.eos_ids, self.embeddings, self.logit_weights = [], [], [], []

        for sequence in token_sequences:
            self.start_tokens.append(nn.Parameter(torch.randn(dim)))
            self.eos_ids.append(sequence.vocab_size)

            vocab_size_with_eos = sequence.vocab_size + 1

            self.embeddings.append(nn.Embedding(vocab_size_with_eos * sequence.tokens_per_step, dim))
            self.logit_weights.append(nn.Parameter(torch.randn(sequence.tokens_per_step, vocab_size_with_eos, dim)))

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            heads = heads,
            attn_dropout = attn_dropout,
            ff_dropout = ff_dropout,
            cross_attend = has_condition and not cond_as_self_attn_prefix,
            cond_as_self_attn_prefix = cond_as_self_attn_prefix,
            grad_shrink_alpha = grad_shrink_alpha,
            **kwargs
        )

    @property
    def device(self):
        return next(self.parameters()).device

    def forward(self,
        *,
        all_token_ids: List[torch.Tensor],
        self_attn_mask = None,
        # text: Optional[List[str]] = None,
        # text_embeds = None,
        cond_drop_prob = None,
        return_only_final_seq_logits=False
    ):
        """
        all_token_ids: List of tensors containing token ids. Each element can either be 2 dimensional (batch_size, sequence_length) or 3 dimensional (batch_size, tokens_per_step, sequence_length)
                       Each element in list corresponds to one token sequence in self.token_sequences (e.g. semantic, coarse acoustic, fine acoustic, etc.)
        """
        
        b, device = all_token_ids[0].shape[0], self.device

        all_token_ids = map(lambda t: rearrange(t, 'b ... -> b (...)'), all_token_ids)

        assert len(all_token_ids) == len(self.token_sequences) == len(self.embeddings)

        tokens = []
        start_tokens = []
        split_at = []
        for sequence, token_ids, embedding, start_token in zip(self.token_sequences, token_ids, self.embeddings, self.start_tokens):
            # iterate over token sequences

            # add offsets
            if sequence.tokens_per_step > 1:
                offsets = sequence.vocab_size * torch.arange(sequence.tokens_per_step, device = device)
                offsets = repeat(offsets, 'q -> 1 (n q)', n = ceil_div(token_ids.shape[-1], sequence.tokens_per_step))
                offsets = offsets[:, :token_ids.shape[-1]]
                token_ids = token_ids + offsets

            # get embeddings and prepare for next step
            token_embeddings = embedding(token_ids)

            tokens.append(token_embeddings)
            start_tokens.append(repeat(start_token, 'd -> b 1 d', b = b))

            n_tokens = token_embeddings.shape[1]
            split_at.append(n_tokens if len(split_at) == 0 else split_at[-1] + n_tokens)

        tokens = list(itertools.chain(*zip(start_tokens, tokens))) # [start_1, tokens_1, start_2, tokens_2, ...]
        tokens = torch.cat(tokens, dim = 1)

        tokens = self.transformer(tokens, self_attn_mask = self_attn_mask)

        split_at = split_at[:-1] # remove last element (total number of tokens)
        
        all_pred_tokens = torch.tensor_split(tokens, [sequence.tokens_per_step for sequence in self.token_sequences], dim = 1)

        # get logits

        all_logits = []
        assert len(all_pred_tokens) == len(self.token_sequences) == len(self.logit_weights)

        for index, (sequence, pred_tokens, seq_logit_weights) in enumerate(zip(self.token_sequences, all_pred_tokens, self.logit_weights)):
            if not return_only_final_seq_logits or index == len(self.token_sequences) - 1:
                n = pred_tokens.shape[1]
                nq = round_down_nearest_multiple(n, sequence.tokens_per_step)

                pred_tokens_groupable, pred_tokens_remainder = pred_tokens[:, :nq], pred_tokens[:, nq:]

                pred_tokens_groupable = rearrange(pred_tokens_groupable, 'b (n q) d -> b n q d', q = sequence.tokens_per_step)

                pred_logits_groupable = einsum('q c d, b n q d -> b n q c', seq_logit_weights, pred_tokens_groupable)

                pred_logits_groupable = rearrange(pred_logits_groupable, 'b n q c -> b (n q) c')

                remainder_num_tokens_in_step = pred_tokens_remainder.shape[1]

                if remainder_num_tokens_in_step > 0:
                    pred_logits_remainder = einsum('q c d, b q d -> b q c', seq_logit_weights[:remainder_num_tokens_in_step], pred_tokens_remainder)
                    pred_logits = torch.cat((pred_logits_groupable, pred_logits_remainder), dim = 1)
                else:
                    pred_logits = pred_logits_groupable 

                all_logits.append(pred_logits)
            else:
                all_logits.append(None)

        return all_logits

    def forward_with_cond_scale(
        self,
        *args,
        cond_scale = 3,
        **kwargs
    ):
        """Doesn't do anything without the AudioLM-pytorch text conditioning implementation"""

        logits = self.forward(*args, cond_drop_prob = 0., **kwargs)

        if cond_scale == 1 or not self.has_condition:
            return logits

        null_logits = self.forward(*args, cond_drop_prob = 1., **kwargs)

        scaled_logits = []

        for seq_logits, null_seq_logits in zip(logits, null_logits):
            if seq_logits is None:
                scaled_logits.append(None)
            else:
                scaled_logits.append(null_seq_logits + (seq_logits - null_seq_logits) * cond_scale)

        return scaled_logits


@beartype
class MusicLM(nn.Module):
    def __init__(
        self,
        *,
        wav2vec: Optional[Union[FairseqVQWav2Vec, HubertWithKmeans]],
        clap: ClapQuantized,
        soundstream: SoundStream,
        semantic_transformer: SemanticTransformer,
        coarse_transformer: CoarseTransformer,
        fine_transformer: FineTransformer,
        unique_consecutive=True
    ):
        super().__init__()

        assert semantic_transformer.num_semantic_tokens == coarse_transformer.num_semantic_tokens
        assert coarse_transformer.codebook_size == fine_transformer.codebook_size
        assert coarse_transformer.num_coarse_quantizers == fine_transformer.num_coarse_quantizers

        self.semantic_has_condition = semantic_transformer.has_condition
        self.coarse_has_condition = coarse_transformer.has_condition
        self.fine_has_condition = fine_transformer.has_condition
        self.needs_text = any(
            [self.semantic_has_condition, self.coarse_has_condition, self.fine_has_condition])

        self.semantic = SemanticTransformerWrapper(
            wav2vec=wav2vec,
            transformer=semantic_transformer,
            unique_consecutive=unique_consecutive
        )

        self.coarse = CoarseTransformerWrapper(
            wav2vec=wav2vec,
            soundstream=soundstream,
            transformer=coarse_transformer,
            unique_consecutive=unique_consecutive
        )

        self.fine = FineTransformerWrapper(
            soundstream=soundstream,
            transformer=fine_transformer
        )

        self.clap = clap

    @property
    def device(self):
        return next(self.parameters()).device

    @eval_decorator
    @torch.no_grad()
    def forward(
        self,
        *,
        batch_size=1,
        text: Optional[List[str]] = None,
        prime_wave=None,
        max_length=2048,
        return_coarse_generated_wave=False,
        mask_out_generated_fine_tokens=False
    ):
        """
        Given a condition text, generate a wave.
        Sample: a dict containing all the data of current sample.
        audio_data: a tensor of shape (T) containing audio data.
        max_len: the maximum length of audio data.
        data_truncating: the method of truncating data.
        data_filling: the method of filling data.
        audio_cfg: a dict containing audio configuration. Comes from model_cfg['audio_cfg'].
        """
        assert exists(text), 'text needs to be passed in if one of the transformer requires conditioning'

        if exists(prime_wave):
            prime_wave = prime_wave.to(self.device)

        semantic_token_ids = self.semantic.generate(
            text=text if self.semantic_has_condition else None,
            batch_size=batch_size,
            prime_wave=prime_wave,
            max_length=max_length
        )

        coarse_token_ids_or_recon_wave = self.coarse.generate(
            text=text if self.coarse_has_condition else None,
            semantic_token_ids=semantic_token_ids,
            reconstruct_wave=return_coarse_generated_wave
        )

        if return_coarse_generated_wave:
            return coarse_token_ids_or_recon_wave

        generated_wave = self.fine.generate(
            text=text if self.fine_has_condition else None,
            coarse_token_ids=coarse_token_ids_or_recon_wave,
            reconstruct_wave=True,
            mask_out_generated_fine_tokens=mask_out_generated_fine_tokens
        )

        return generated_wave
