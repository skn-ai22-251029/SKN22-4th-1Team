class DurService:
    @staticmethod
    def filter_and_process_dur(dur_data_list: list):
        """
        주인님, dur_seq가 없는 데이터는 특정 금기(병용/연령 등)에 걸리지 않는 
        '단일 성분 마스터 정보'입니다. 이를 무시하지 않고 '대체제 검색용 키'로 활용합니다.
        """
        refined_data = []
        for item in dur_data_list:
            if not item.get("dur_seq"):
                # dur_seq가 없으면 매핑 전용 데이터로 태그
                item["data_type"] = "MAPPING_ONLY" 
                item["safety_status"] = "SAFE"
            else:
                item["data_type"] = "CONTRAINDICATION"
                item["safety_status"] = "CHECK_REQUIRED"
            refined_data.append(item)
        return refined_data