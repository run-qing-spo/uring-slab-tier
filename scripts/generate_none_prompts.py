#!/usr/bin/env python3
"""为 none 基线生成可复用、严格定长且互不相同的 prompt JSONL。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from transformers import AutoTokenizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=128)
    parser.add_argument("--tokens", type=int, default=128)
    parser.add_argument("--block-size", type=int, default=16)
    return parser.parse_args()


def encode(tokenizer, text: str) -> list[int]:
    return tokenizer.encode(text, add_special_tokens=True)


def make_prompt(tokenizer, index: int, target_tokens: int) -> str:
    # 每条 prompt 的开头不同，余下位置用一个普通词 token 填满。
    header = f"None baseline prompt {index:06d}."
    header_ids = tokenizer.encode(header, add_special_tokens=False)
    filler_ids = tokenizer.encode(" experiment", add_special_tokens=False)
    if len(filler_ids) != 1:
        raise RuntimeError("当前 tokenizer 中填充词不是单 token")

    special_tokens = tokenizer.num_special_tokens_to_add(pair=False)
    content_tokens = target_tokens - special_tokens
    if len(header_ids) > content_tokens:
        raise ValueError(f"prompt header 已超过目标长度：index={index}")

    token_ids = header_ids + filler_ids * (content_tokens - len(header_ids))
    prompt = tokenizer.decode(
        token_ids,
        skip_special_tokens=False,
        clean_up_tokenization_spaces=False,
    )
    actual_ids = encode(tokenizer, prompt)
    if len(actual_ids) != target_tokens:
        raise RuntimeError(
            f"prompt round-trip 后不是 {target_tokens} tokens："
            f"index={index}, actual={len(actual_ids)}"
        )
    return prompt


def main() -> None:
    args = parse_args()
    if args.count <= 0 or args.tokens <= 0 or args.block_size <= 0:
        raise ValueError("count、tokens 和 block-size 必须为正数")
    if args.tokens < args.block_size:
        raise ValueError("tokens 不能小于 block-size")

    tokenizer = AutoTokenizer.from_pretrained(
        args.model_snapshot,
        local_files_only=True,
        trust_remote_code=False,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")

    seen_prompts: set[str] = set()
    seen_hashes: set[str] = set()
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            for index in range(args.count):
                prompt = make_prompt(tokenizer, index, args.tokens)
                digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                if prompt in seen_prompts or digest in seen_hashes:
                    raise RuntimeError(f"生成了重复 prompt：index={index}")
                seen_prompts.add(prompt)
                seen_hashes.add(digest)
                record = {
                    "prompt_id": f"none-{index:06d}",
                    "prompt": prompt,
                    "token_count": args.tokens,
                    "sha256": digest,
                }
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 写完后重新读取和分词，避免截断文件或序列化改变文本而未被发现。
        verified_ids: set[str] = set()
        verified_hashes: set[str] = set()
        first_blocks: set[tuple[int, ...]] = set()
        with temporary.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                record = json.loads(line)
                prompt_id = record["prompt_id"]
                prompt = record["prompt"]
                digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                token_ids = encode(tokenizer, prompt)
                if len(token_ids) != args.tokens:
                    raise RuntimeError(f"第 {line_number} 行 token 数校验失败")
                if digest != record["sha256"]:
                    raise RuntimeError(f"第 {line_number} 行哈希校验失败")
                if prompt_id in verified_ids or digest in verified_hashes:
                    raise RuntimeError(f"第 {line_number} 行唯一性校验失败")

                # 首个完整 KV block 必须不同，否则请求间会出现 prefix-cache hit。
                first_block = tuple(token_ids[: args.block_size])
                if first_block in first_blocks:
                    raise RuntimeError(
                        f"第 {line_number} 行的首个 KV block 与其他 prompt 重复"
                    )
                first_blocks.add(first_block)
                verified_ids.add(prompt_id)
                verified_hashes.add(digest)

        if len(verified_ids) != args.count:
            raise RuntimeError(
                f"prompt 数量校验失败：expected={args.count}, "
                f"actual={len(verified_ids)}"
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"已生成 {args.count} 条唯一 prompt：{args.tokens} tokens/条，{output}")


if __name__ == "__main__":
    main()
