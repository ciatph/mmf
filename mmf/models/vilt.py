# Copyright (c) Facebook, Inc. and its affiliates.
import collections.abc
import logging
from dataclasses import dataclass, field
from typing import Any, Dict

import torch
from mmf.common.registry import registry
from mmf.models.base_model import BaseModel
from mmf.models.transformers.heads.utils import HeadsDict
from mmf.modules.encoders import TransformerEncoder, ViTEncoder
from mmf.modules.losses import MMFLoss
from mmf.utils.build import build_encoder
from mmf.utils.modeling import get_bert_configured_parameters
from omegaconf import MISSING
from torch import Tensor, nn


logger = logging.getLogger()


class ViLTImageEmbedding(nn.Module):
    """
    Patch embedding used for ViLT.
    https://arxiv.org/pdf/2102.03334.pdf
    Implementation based off
    https://github.com/dandelin/ViLT/blob/master/vilt/modules/vilt_module.py
    Using huggingface ViT modules.
    Can be built with random init or the embeddings weights from an exisiting
    ViT model from huggingface. Model list: availible at
    https://huggingface.co/models?other=vit&sort=downloads
    """

    @dataclass
    class Config:
        image_size: list = field(default_factory=lambda: [224, 224])
        hidden_dropout_prob: float = 0
        hidden_dim: int = 768
        patch_size: int = 16
        num_channels: int = 3
        random_init: bool = True
        pretrained_model_name: str = "google/vit-base-patch16-224"

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.embedding = ViTEncoder(self.config).embeddings
        self.token_type_embeddings = nn.Embedding(1, self.config.hidden_dim)

    def forward(self, image):
        if image.dim() == 5:
            image = image.permute(1, 0, 2, 3, 4).flatten(start_dim=0, end_dim=1)

        img_embeddings = self.embedding(image)

        img_segment_ids = torch.zeros(
            img_embeddings.size()[:-1],
            dtype=img_embeddings.dtype,
            device=img_embeddings.device,
        ).long()
        img_type_embed = self.token_type_embeddings(img_segment_ids)
        img_embeddings = img_embeddings + img_type_embed
        return img_embeddings


class ViLTTextEmbedding(nn.Module):
    @dataclass
    class Config:
        hidden_dim: int = 768
        hidden_size: int = 768
        bert_model_name: str = "bert-base-uncased"

    def __init__(self, config: Config):

        super().__init__()
        self.config = config
        text_encoder = TransformerEncoder(self.config)
        self.text_embeddings = text_encoder.embeddings
        encoder_output_dim = self.config.hidden_dim
        self.text_projection = nn.Linear(
            text_encoder.config.hidden_size, encoder_output_dim
        )

    def forward(self, input_ids, segment_ids):
        text_embedding = self.text_embeddings(input_ids, token_type_ids=segment_ids)
        text_embedding = self.text_projection(text_embedding)
        return text_embedding


@registry.register_model("vilt")
class ViLT(BaseModel):
    @dataclass
    class Config(BaseModel.Config):
        name: str = "ViLT"
        text_embeddings: ViLTTextEmbedding.Config = ViLTTextEmbedding.Config()
        image_encoder: Any = MISSING

    @classmethod
    def config_path(cls):
        return "configs/models/vilt/defaults.yaml"

    def build(self):
        self.text_embeddings = ViLTTextEmbedding(self.config.text_embeddings)
        self.image_embeddings = ViLTImageEmbedding(self.config.image_encoder.params)
        self.encoder = build_encoder(self.config.image_encoder)

        head_configs = self.config.get("heads", {})
        self.tasks = self.config.get("tasks", head_configs.keys())
        if isinstance(self.tasks, str):
            self.tasks = self.tasks.split(",")

        self.losses = nn.ModuleDict()
        self.heads_dict = HeadsDict.build_heads(head_configs, self.tasks, self.losses)
        self.modality_keys = self.modality_type = ["text", "image"]

    def init_losses(self):
        loss_configs = self.config.get("losses", {})
        for loss_name, loss_config in loss_configs.items():
            self.losses[loss_name] = MMFLoss(loss_config)

    def forward(self, sample_list: Dict[str, Tensor]) -> Dict[str, Tensor]:
        text_embedding = self.text_embeddings(
            sample_list["input_ids"], sample_list["segment_ids"]
        )
        image_embedding = self.image_embeddings(sample_list["image"])
        self.preprocess_sample(sample_list, image_embedding)

        # Feed through encoder
        embeddings = torch.cat([text_embedding, image_embedding], dim=1)
        attention_mask = self.get_attention_mask(
            sample_list, text_embedding, image_embedding
        )
        sequence, _ = self.encoder(embeddings, attention_mask=attention_mask)
        if sequence.dim() != 3:
            sequence = sequence.unsqueeze(1)

        outputs = self.heads_dict(sample_list["dataset_name"], sequence, sample_list)
        return outputs

    def preprocess_sample(self, sample_list, image_embedding):
        head_names = self.heads_dict.head_names
        if isinstance(head_names, collections.abc.Mapping):
            head_names = head_names[sample_list.dataset_name]

        head_string = " ".join(head_names)
        prepare_itm = "itm" in head_string
        prepare_mlm = "mlm" in head_string

        if prepare_itm:
            sample_list["itm_labels"] = self._infer_itm_labels(sample_list)
        if prepare_mlm:
            sample_list["mlm_labels"] = self._infer_mlm_labels(
                sample_list, image_embedding.size()[:-1]
            )
            self._encode_mlm(sample_list, image_embedding)

    def get_optimizer_parameters(self, config):
        if hasattr(self.encoder, "get_optimizer_parameters"):
            params = self.encoder.get_optimizer_parameters(config)
        else:
            params = [{"params": self.encoder.parameters()}]
        params += get_bert_configured_parameters(self.text_embeddings)
        params += get_bert_configured_parameters(self.heads_dict)
        params += [{"params": self.image_embeddings.parameters()}]
        return params

    def get_attention_mask(self, sample_list, text_embedding, image_embedding):
        text_mask = getattr(sample_list, "input_mask", None)
        image_mask = getattr(sample_list, "image_mask", None)

        if text_mask is None and image_mask is None:
            return None

        if text_mask is None:
            text_mask = torch.ones(
                text_embedding.size()[:-1],
                dtype=text_embedding.dtype,
                device=text_embedding.device,
            )

        if image_mask is None:
            image_mask = torch.ones(
                image_embedding.size()[:-1],
                dtype=image_embedding.dtype,
                device=image_embedding.device,
            )

        attention_mask = torch.cat((text_mask, image_mask), dim=-1)
        return attention_mask

    def _infer_itm_labels(self, sample_list):
        input_ids = sample_list["input_ids"]
        itm_labels = {}
        if "is_correct" in sample_list:
            itm_labels["is_correct"] = sample_list["is_correct"]
        else:
            itm_labels["is_correct"] = torch.tensor(
                True, dtype=torch.long, device=input_ids.device
            )

        return itm_labels

    def _infer_mlm_labels(self, sample_list, image_embeddings_size):
        input_ids = sample_list["input_ids"]
        mlm_labels = {}
        current_text_idx = 0
        if "lm_label_ids" in sample_list:
            if sample_list["lm_label_ids"].dim() > 2:
                mlm_labels["text"] = sample_list["lm_label_ids"][:, current_text_idx]
                current_text_idx += 1
            else:
                mlm_labels["text"] = sample_list["lm_label_ids"]
        else:
            mlm_labels["text"] = torch.full(
                input_ids.size(),
                fill_value=-1,
                dtype=torch.long,
                device=input_ids.device,
            )
        mlm_labels["image"] = torch.full(
            image_embeddings_size,
            fill_value=-1,
            dtype=torch.long,
            device=input_ids.device,
        )
        mlm_labels["combined_labels"] = torch.cat(
            [mlm_labels["text"], mlm_labels["image"]], dim=-1
        )
        return mlm_labels

    def _encode_mlm(self, sample_list, image_embedding):
        assert "lm_label_ids" in sample_list

        input_ids = sample_list.get("input_ids_masked", sample_list.input_ids)
        segment_ids = sample_list.segment_ids
        text_embedding = self.text_embeddings(input_ids, segment_ids)

        embeddings = torch.cat([image_embedding, text_embedding], dim=1)
        attention_mask = self.get_attention_mask(
            sample_list, text_embedding, image_embedding
        )
        sequence, _ = self.encoder(embeddings, attention_mask=attention_mask)
        if sequence.dim() != 3:
            sequence = sequence.unsqueeze(1)

        sample_list.hs_masked_for_mlm = sequence
